from gibson2.objects.visual_marker import VisualMarker
from gibson2.utils.mesh_util import quat2rotmat, xyzw2wxyz, xyz2mat
from gibson2.utils.semantics_utils import get_class_name_to_class_id
from gibson2.utils.constants import SemanticClass, PyBulletSleepState
from gibson2.render.mesh_renderer.mesh_renderer_cpu import MeshRenderer
from gibson2.render.mesh_renderer.mesh_renderer_vr import MeshRendererVR, VrSettings
from gibson2.render.mesh_renderer.mesh_renderer_settings import MeshRendererSettings
from gibson2.render.mesh_renderer.instances import InstanceGroup, Instance, Robot
from gibson2.render.mesh_renderer.mesh_renderer_tensor import MeshRendererG2G
from gibson2.render.viewer import Viewer, ViewerVR, ViewerSimple
from gibson2.object_states.factory import get_states_by_dependency_order
from gibson2.objects.articulated_object import ArticulatedObject, URDFObject
from gibson2.scenes.igibson_indoor_scene import InteractiveIndoorScene
from gibson2.scenes.scene_base import Scene
from gibson2.robots.robot_base import BaseRobot
from gibson2.objects.object_base import Object
from gibson2.objects.particles import ParticleSystem
from gibson2.utils.utils import quatXYZWFromRotMat, rotate_vector_3d
from gibson2.utils.assets_utils import get_ig_avg_category_specs

import pybullet as p
import gibson2
import json
import os
import numpy as np
import platform
import logging
import time
from time import sleep


class Simulator:
    """
    Simulator class is a wrapper of physics simulator (pybullet) and MeshRenderer, it loads objects into
    both pybullet and also MeshRenderer and syncs the pose of objects and robot parts.
    """

    def __init__(self,
                 gravity=9.8,
                 physics_timestep=1 / 120.0,
                 render_timestep=1 / 30.0,
                 use_fixed_fps=False,
                 mode='gui',
                 image_width=128,
                 image_height=128,
                 vertical_fov=90,
                 device_idx=0,
                 render_to_tensor=False,
                 rendering_settings=MeshRendererSettings(),
                 vr_settings=VrSettings()):
        """
        :param gravity: gravity on z direction.
        :param physics_timestep: timestep of physical simulation, p.stepSimulation()
        :param render_timestep: timestep of rendering, and Simulator.step() function
        :param use_variable_step_num: whether to use a fixed (1) or variable physics step number
        :param mode: choose mode from gui, headless, iggui (only open iGibson UI), or pbgui(only open pybullet UI)
        :param image_width: width of the camera image
        :param image_height: height of the camera image
        :param vertical_fov: vertical field of view of the camera image in degrees
        :param device_idx: GPU device index to run rendering on
        :param render_to_tensor: Render to GPU tensors
        disable it when you want to run multiple physics step but don't need to visualize each frame
        :param rendering_settings: settings to use for mesh renderer
        :param vr_settings: settings to use for VR in simulator and MeshRendererVR
        """
        # physics simulator
        self.gravity = gravity
        self.physics_timestep = physics_timestep
        self.render_timestep = render_timestep
        self.use_fixed_fps = use_fixed_fps
        self.mode = mode

        self.scene = None

        self.particle_systems = []

        # TODO: remove this, currently used for testing only
        self.objects = []

        plt = platform.system()
        if plt == 'Darwin' and self.mode == 'gui':
            self.mode = 'iggui'  # for mac os disable pybullet rendering
            logging.warn('Rendering both iggui and pbgui is not supported on mac, choose either pbgui or '
                         'iggui. Default to iggui.')

        self.use_pb_renderer = False
        self.use_ig_renderer = False
        self.use_vr_renderer = False
        self.use_simple_viewer = False

        if self.mode in ['gui', 'iggui']:
            self.use_ig_renderer = True

        if self.mode in ['gui', 'pbgui']:
            self.use_pb_renderer = True

        if self.mode in ['vr']:
            self.use_vr_renderer = True

        if self.mode in ['simple']:
            self.use_simple_viewer = True

        # Starting position for the VR (default set to None if no starting position is specified by the user)
        self.vr_start_pos = None
        self.eye_tracking_data = None
        self.max_haptic_duration = 4000
        self.image_width = image_width
        self.image_height = image_height
        self.vertical_fov = vertical_fov
        self.device_idx = device_idx
        self.render_to_tensor = render_to_tensor

        self.optimized_renderer = rendering_settings.optimized
        self.rendering_settings = rendering_settings
        self.viewer = None
        self.vr_settings = vr_settings
        self.vr_overlay_initialized = False
        # We must be using the Simulator's vr mode and have use_vr set to true in the settings to access the VR context
        self.can_access_vr_context = self.use_vr_renderer and self.vr_settings.use_vr
        # If we are using VR, inherit fixed_fps setting from VrSettings
        if self.can_access_vr_context:
            self.use_fixed_fps = self.vr_settings.use_fixed_fps

        # Get expected duration of frame
        self.fixed_frame_dur = 1/float(self.vr_settings.vr_fps)
        # Duration of a vsync frame - assumes 90Hz refresh rate
        self.vsync_frame_dur = 11.11e-3
        # Get expected number of vsync frames per iGibson frame
        # Note: currently assumes a 90Hz VR system
        self.vsync_frame_num = int(
            round(self.fixed_frame_dur / self.vsync_frame_dur))
        # Total amount of time we want non-blocking actions to take each frame
        # This leaves 1 entire vsync frame for blocking, to make sure we don't wait too long
        # Add 1e-3 to go halfway into the next frame
        self.non_block_frame_time = (
            self.vsync_frame_num - 1) * self.vsync_frame_dur + 1e-3
        # Number of physics steps based on fixed VR fps
        # Use integer division to guarantee we don't exceed 1.0 realtime factor
        # It is recommended to use an FPS that is a multiple of the timestep
        self.num_phys_steps = max(
            1, int(self.fixed_frame_dur/self.physics_timestep))
        # Timing variables for functions called outside of step() that also take up frame time
        self.frame_end_time = None

        # Variables for data saving and replay in VR
        self.last_physics_timestep = -1
        self.last_render_timestep = -1
        self.last_physics_step_num = -1
        self.last_frame_dur = -1
        self.frame_count = 0

        self.load()

        self.class_name_to_class_id = get_class_name_to_class_id()
        self.body_links_awake = 0
        # First sync always sync all objects (regardless of their sleeping states)
        self.first_sync = True
        # List of categories that can be grasped by assisted grasping
        self.assist_grasp_category_allow_list = []
        self.gen_assisted_grasping_categories()

        self.object_state_types = get_states_by_dependency_order()

    def set_timestep(self, physics_timestep, render_timestep):
        """
        Set physics timestep and render (action) timestep

        :param physics_timestep: physics timestep for pybullet
        :param render_timestep: rendering timestep for renderer
        """
        self.physics_timestep = physics_timestep
        self.render_timestep = render_timestep
        p.setTimeStep(self.physics_timestep)

    def set_render_timestep(self, render_timestep):
        """
        :param render_timestep: render timestep to set in the Simulator
        """
        self.render_timestep = render_timestep

    def add_viewer(self):
        """
        Attach a debugging viewer to the renderer.
        This will make the step much slower so should be avoided when training agents
        """
        if self.use_vr_renderer:
            self.viewer = ViewerVR(self.vr_settings.use_companion_window)
        elif self.use_simple_viewer:
            self.viewer = ViewerSimple()
        else:
            self.viewer = Viewer(simulator=self, renderer=self.renderer)
        self.viewer.renderer = self.renderer

    def reload(self):
        """
        Destroy the MeshRenderer and physics simulator and start again.
        """
        self.disconnect()
        self.load()

    def load(self):
        """
        Set up MeshRenderer and physics simulation client. Initialize the list of objects.
        """
        if self.render_to_tensor:
            self.renderer = MeshRendererG2G(width=self.image_width,
                                            height=self.image_height,
                                            vertical_fov=self.vertical_fov,
                                            device_idx=self.device_idx,
                                            rendering_settings=self.rendering_settings)
        elif self.use_vr_renderer:
            self.renderer = MeshRendererVR(
                rendering_settings=self.rendering_settings, vr_settings=self.vr_settings)
        else:
            self.renderer = MeshRenderer(width=self.image_width,
                                         height=self.image_height,
                                         vertical_fov=self.vertical_fov,
                                         device_idx=self.device_idx,
                                         rendering_settings=self.rendering_settings)

        # print("******************PyBullet Logging Information:")
        if self.use_pb_renderer:
            self.cid = p.connect(p.GUI)
        else:
            self.cid = p.connect(p.DIRECT)

        # Simulation reset is needed for deterministic action replay
        if self.vr_settings.reset_sim:
            p.resetSimulation()
            p.setPhysicsEngineParameter(deterministicOverlappingPairs=1)
        if self.mode == 'vr':
            p.setPhysicsEngineParameter(numSolverIterations=100)
        p.setTimeStep(self.physics_timestep)
        p.setGravity(0, 0, -self.gravity)
        p.setPhysicsEngineParameter(enableFileCaching=0)
        self.visual_objects = {}
        self.robots = []
        self.scene = None
        if (self.use_ig_renderer or self.use_vr_renderer or self.use_simple_viewer) and not self.render_to_tensor:
            self.add_viewer()

    def load_without_pybullet_vis(load_func):
        """
        Load without pybullet visualizer
        """
        def wrapped_load_func(*args, **kwargs):
            p.configureDebugVisualizer(p.COV_ENABLE_RENDERING, False)
            res = load_func(*args, **kwargs)
            p.configureDebugVisualizer(p.COV_ENABLE_RENDERING, True)
            return res
        return wrapped_load_func

    @load_without_pybullet_vis
    def import_scene(self,
                     scene,
                     texture_scale=1.0,
                     load_texture=True,
                     render_floor_plane=False,
                     class_id=SemanticClass.SCENE_OBJS,
                     ):
        """
        Import a scene into the simulator. A scene could be a synthetic one or a realistic Gibson Environment.

        :param scene: Scene object
        :param texture_scale: Option to scale down the texture for rendering
        :param load_texture: If you don't need rgb output, texture loading could be skipped to make rendering faster
        :param render_floor_plane: Whether to render the additionally added floor plane
        :param class_id: Class id for rendering semantic segmentation
        :return: pybullet body ids from scene.load function
        """
        assert isinstance(scene, Scene) and not isinstance(scene, InteractiveIndoorScene), \
            'import_scene can only be called with Scene that is not InteractiveIndoorScene'
        # Load the scene. Returns a list of pybullet ids of the objects loaded that we can use to
        # load them in the renderer
        new_object_pb_ids = scene.load()
        self.objects += new_object_pb_ids

        # Load the objects in the renderer
        for new_object_pb_id in new_object_pb_ids:
            self.load_object_in_renderer(new_object_pb_id, class_id=class_id, texture_scale=texture_scale,
                                         load_texture=load_texture, render_floor_plane=render_floor_plane,
                                         use_pbr=False, use_pbr_mapping=False)

            # TODO: add instance renferencing for iG v1 scenes

        self.scene = scene
        return new_object_pb_ids

    @load_without_pybullet_vis
    def import_ig_scene(self, scene):
        """
        Import scene from iGSDF class

        :param scene: iGSDFScene instance
        :return: pybullet body ids from scene.load function
        """
        assert isinstance(scene, InteractiveIndoorScene), \
            'import_ig_scene can only be called with InteractiveIndoorScene'
        new_object_ids = scene.load()
        self.objects += new_object_ids
        if scene.texture_randomization:
            # use randomized texture
            for body_id, visual_mesh_to_material in \
                    zip(new_object_ids, scene.visual_mesh_to_material):
                shadow_caster = True
                if scene.objects_by_id[body_id].category == 'ceilings':
                    shadow_caster = False
                class_id = self.class_name_to_class_id.get(
                    scene.objects_by_id[body_id].category, SemanticClass.SCENE_OBJS)
                self.load_articulated_object_in_renderer(
                    body_id,
                    class_id=class_id,
                    visual_mesh_to_material=visual_mesh_to_material,
                    shadow_caster=shadow_caster,
                    physical_object=scene.objects_by_id[body_id])
        else:
            # use default texture
            for body_id in new_object_ids:
                use_pbr = True
                use_pbr_mapping = True
                shadow_caster = True
                if scene.scene_source == 'IG':
                    if scene.objects_by_id[body_id].category in ['walls', 'floors', 'ceilings']:
                        use_pbr = False
                        use_pbr_mapping = False
                if scene.objects_by_id[body_id].category == 'ceilings':
                    shadow_caster = False
                class_id = self.class_name_to_class_id.get(
                    scene.objects_by_id[body_id].category, SemanticClass.SCENE_OBJS)
                self.load_articulated_object_in_renderer(
                    body_id,
                    class_id=body_id,
                    use_pbr=use_pbr,
                    use_pbr_mapping=use_pbr_mapping,
                    shadow_caster=shadow_caster,
                    physical_object=scene.objects_by_id[body_id])
        self.scene = scene

        return new_object_ids

    @load_without_pybullet_vis
    def import_particle_system(self,
                               obj,
                               class_id=SemanticClass.USER_ADDED_OBJS,
                               use_pbr=False,
                               use_pbr_mapping=False,
                               shadow_caster=True):
        """
        Import an object into the simulator
        :param obj: ParticleSystem to load
        :param class_id: Class id for rendering semantic segmentation
        :param use_pbr: Whether to use pbr, default to False
        :param use_pbr_mapping: Whether to use pbr mapping, default to False
        :param shadow_caster: Whether to cast shadow
        """

        assert isinstance(obj, ParticleSystem), \
            'import_particle_system can only be called with ParticleSystem'

        new_object_pb_ids = []
        for o in obj.get_particles():
            particle_pb_id = self.import_object(o,
                                                class_id=class_id,
                                                use_pbr=use_pbr,
                                                use_pbr_mapping=use_pbr_mapping,
                                                shadow_caster=shadow_caster)
            new_object_pb_ids.append(particle_pb_id)

        self.particle_systems.append(obj)

        return new_object_pb_ids

    @load_without_pybullet_vis
    def import_object(self,
                      obj,
                      class_id=SemanticClass.USER_ADDED_OBJS,
                      use_pbr=True,
                      use_pbr_mapping=True,
                      shadow_caster=True):
        """
        Import an object into the simulator

        :param obj: Object to load
        :param class_id: Class id for rendering semantic segmentation
        :param use_pbr: Whether to use pbr
        :param use_pbr_mapping: Whether to use pbr mapping
        :param shadow_caster: Whether to cast shadow
        """
        assert isinstance(obj, Object), \
            'import_object can only be called with Object'

        if isinstance(obj, VisualMarker):
            # Marker objects can be imported without a scene.
            new_object_pb_id_or_ids = obj.load()
        else:
            # Non-marker objects require a Scene to be imported.
            assert self.scene is not None, "A scene must be imported before additional objects can be imported."
            # Load the object in pybullet. Returns a pybullet id that we can use to load it in the renderer
            new_object_pb_id_or_ids = self.scene.add_object(
                obj, _is_call_from_simulator=True)

        # If no new bodies are immediately imported into pybullet, we have no rendering steps.
        if new_object_pb_id_or_ids is None:
            return None

        if isinstance(new_object_pb_id_or_ids, list):
            new_object_pb_ids = new_object_pb_id_or_ids
        else:
            new_object_pb_ids = [new_object_pb_id_or_ids]
        self.objects += new_object_pb_ids

        for new_object_pb_id in new_object_pb_ids:
            if isinstance(obj, ArticulatedObject) or isinstance(obj, URDFObject):
                self.load_articulated_object_in_renderer(
                    new_object_pb_id,
                    class_id,
                    use_pbr=use_pbr,
                    use_pbr_mapping=use_pbr_mapping,
                    shadow_caster=shadow_caster,
                    physical_object=obj)
            else:
                softbody = obj.__class__.__name__ == 'SoftObject'
                self.load_object_in_renderer(
                    new_object_pb_id,
                    class_id,
                    softbody,
                    use_pbr=use_pbr,
                    use_pbr_mapping=use_pbr_mapping,
                    shadow_caster=shadow_caster,
                    physical_object=obj)

        return new_object_pb_id_or_ids

    @load_without_pybullet_vis
    def load_object_in_renderer(self,
                                object_pb_id,
                                class_id=None,
                                softbody=False,
                                texture_scale=1.0,
                                load_texture=True,
                                render_floor_plane=False,
                                use_pbr=True,
                                use_pbr_mapping=True,
                                shadow_caster=True,
                                physical_object=None,
                                ):
        """
        Load the object into renderer

        :param object_pb_id: pybullet body id
        :param class_id: Class id for rendering semantic segmentation
        :param softbody: Whether the object is soft body
        :param texture_scale: Texture scale
        :param load_texture: If you don't need rgb output, texture loading could be skipped to make rendering faster
        :param render_floor_plane: Whether to render the additionally added floor plane
        :param use_pbr: Whether to use pbr
        :param use_pbr_mapping: Whether to use pbr mapping
        :param shadow_caster: Whether to cast shadow
        :param physical_object: The reference to Object class
        """
        for shape in p.getVisualShapeData(object_pb_id):
            id, link_id, type, dimensions, filename, rel_pos, rel_orn, color = shape[:8]
            visual_object = None
            if type == p.GEOM_MESH:
                filename = filename.decode('utf-8')
                if (filename, tuple(dimensions), tuple(rel_pos), tuple(rel_orn)) not in self.visual_objects.keys():
                    self.renderer.load_object(filename,
                                              transform_orn=rel_orn,
                                              transform_pos=rel_pos,
                                              input_kd=color[:3],
                                              scale=np.array(dimensions),
                                              texture_scale=texture_scale,
                                              load_texture=load_texture)
                    self.visual_objects[(filename, tuple(dimensions), tuple(rel_pos), tuple(rel_orn))
                                        ] = len(self.renderer.visual_objects) - 1
                visual_object = self.visual_objects[
                    (filename,
                     tuple(dimensions),
                     tuple(rel_pos),
                     tuple(rel_orn)
                     )]
            elif type == p.GEOM_SPHERE:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/sphere8.obj')
                self.renderer.load_object(
                    filename,
                    transform_orn=rel_orn,
                    transform_pos=rel_pos,
                    input_kd=color[:3],
                    scale=[dimensions[0] / 0.5, dimensions[0] / 0.5, dimensions[0] / 0.5])
                visual_object = len(self.renderer.get_visual_objects()) - 1
            elif type == p.GEOM_CAPSULE or type == p.GEOM_CYLINDER:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/cube.obj')
                self.renderer.load_object(
                    filename,
                    transform_orn=rel_orn,
                    transform_pos=rel_pos,
                    input_kd=color[:3],
                    scale=[dimensions[1] / 0.5, dimensions[1] / 0.5, dimensions[0]])
                visual_object = len(self.renderer.get_visual_objects()) - 1
            elif type == p.GEOM_BOX:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/cube.obj')
                self.renderer.load_object(filename,
                                          transform_orn=rel_orn,
                                          transform_pos=rel_pos,
                                          input_kd=color[:3],
                                          scale=np.array(dimensions))
                visual_object = len(self.renderer.visual_objects) - 1
            elif type == p.GEOM_PLANE:
                # By default, we add an additional floor surface to "smooth out" that of the original mesh.
                # Normally you don't need to render this additionally added floor surface.
                # However, if you do want to render it for some reason, you can set render_floor_plane to be True.
                if render_floor_plane:
                    filename = os.path.join(
                        gibson2.assets_path,
                        'models/mjcf_primitives/cube.obj')
                    self.renderer.load_object(filename,
                                              transform_orn=rel_orn,
                                              transform_pos=rel_pos,
                                              input_kd=color[:3],
                                              scale=[100, 100, 0.01])
                    visual_object = len(self.renderer.visual_objects) - 1
            if visual_object is not None:
                self.renderer.add_instance(visual_object,
                                           pybullet_uuid=object_pb_id,
                                           class_id=class_id,
                                           dynamic=True,
                                           softbody=softbody,
                                           use_pbr=use_pbr,
                                           use_pbr_mapping=use_pbr_mapping,
                                           shadow_caster=shadow_caster
                                           )
                if physical_object is not None:
                    physical_object.renderer_instances.append(self.renderer.instances[-1])

    @load_without_pybullet_vis
    def load_articulated_object_in_renderer(self,
                                            object_pb_id,
                                            class_id=None,
                                            visual_mesh_to_material=None,
                                            use_pbr=True,
                                            use_pbr_mapping=True,
                                            shadow_caster=True,
                                            physical_object=None):
        """
        Load the articulated object into renderer

        :param object_pb_id: pybullet body id
        :param class_id: Class id for rendering semantic segmentation
        :param visual_mesh_to_material: mapping from visual mesh to randomizable materials
        :param use_pbr: Whether to use pbr
        :param use_pbr_mapping: Whether to use pbr mapping
        :param shadow_caster: Whether to cast shadow
        :param physical_object: The reference to Object class
        """

        visual_objects = []
        link_ids = []
        poses_rot = []
        poses_trans = []

        for shape in p.getVisualShapeData(object_pb_id):
            id, link_id, type, dimensions, filename, rel_pos, rel_orn, color = shape[:8]
            if type == p.GEOM_MESH:
                filename = filename.decode('utf-8')
                if (filename, tuple(dimensions), tuple(rel_pos), tuple(rel_orn)) not in self.visual_objects.keys():
                    overwrite_material = None
                    if visual_mesh_to_material is not None and filename in visual_mesh_to_material:
                        overwrite_material = visual_mesh_to_material[filename]
                    self.renderer.load_object(
                        filename,
                        transform_orn=rel_orn,
                        transform_pos=rel_pos,
                        input_kd=color[:3],
                        scale=np.array(dimensions),
                        overwrite_material=overwrite_material)
                    self.visual_objects[(filename, tuple(dimensions), tuple(rel_pos), tuple(rel_orn))
                                        ] = len(self.renderer.visual_objects) - 1
                visual_objects.append(
                    self.visual_objects[(filename, tuple(dimensions), tuple(rel_pos), tuple(rel_orn))])
                link_ids.append(link_id)
            elif type == p.GEOM_SPHERE:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/sphere8.obj')
                self.renderer.load_object(
                    filename,
                    transform_orn=rel_orn,
                    transform_pos=rel_pos,
                    input_kd=color[:3],
                    scale=[dimensions[0] / 0.5, dimensions[0] / 0.5, dimensions[0] / 0.5])
                visual_objects.append(
                    len(self.renderer.get_visual_objects()) - 1)
                link_ids.append(link_id)
            elif type == p.GEOM_CAPSULE or type == p.GEOM_CYLINDER:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/cube.obj')
                self.renderer.load_object(
                    filename,
                    transform_orn=rel_orn,
                    transform_pos=rel_pos,
                    input_kd=color[:3],
                    scale=[dimensions[1] / 0.5, dimensions[1] / 0.5, dimensions[0]])
                visual_objects.append(
                    len(self.renderer.get_visual_objects()) - 1)
                link_ids.append(link_id)
            elif type == p.GEOM_BOX:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/cube.obj')
                self.renderer.load_object(filename,
                                          transform_orn=rel_orn,
                                          transform_pos=rel_pos,
                                          input_kd=color[:3],
                                          scale=np.array(dimensions))
                visual_objects.append(
                    len(self.renderer.get_visual_objects()) - 1)
                link_ids.append(link_id)

            if link_id == -1:
                pos, orn = p.getBasePositionAndOrientation(object_pb_id)
            else:
                _, _, _, _, pos, orn = p.getLinkState(object_pb_id, link_id)
            poses_rot.append(np.ascontiguousarray(quat2rotmat(xyzw2wxyz(orn))))
            poses_trans.append(np.ascontiguousarray(xyz2mat(pos)))

        self.renderer.add_instance_group(object_ids=visual_objects,
                                         link_ids=link_ids,
                                         pybullet_uuid=object_pb_id,
                                         class_id=class_id,
                                         poses_trans=poses_trans,
                                         poses_rot=poses_rot,
                                         dynamic=True,
                                         robot=None,
                                         use_pbr=use_pbr,
                                         use_pbr_mapping=use_pbr_mapping,
                                         shadow_caster=shadow_caster)

        if physical_object is not None:
            physical_object.renderer_instances.append(self.renderer.instances[-1])

    def import_non_colliding_objects(self,
                                     objects,
                                     existing_objects=[],
                                     min_distance=0.5):
        """
        Loads objects into the scene such that they don't collide with existing objects.

        :param objects: A dictionary with objects, from a scene loaded with a particular URDF
        :param existing_objects: A list of objects that needs to be kept min_distance away when loading the new objects
        :param min_distance: A minimum distance to require for objects to load
        """
        state_id = p.saveState()
        objects_to_add = []
        for obj_name in objects:
            obj = objects[obj_name]

            # Do not allow duplicate object categories
            if obj.category in self.scene.objects_by_category:
                continue

            add = True
            body_ids = []

            # Filter based on the minimum distance to any existing object
            for idx in range(len(obj.urdf_paths)):
                body_id = p.loadURDF(obj.urdf_paths[idx])
                body_ids.append(body_id)
                transformation = obj.poses[idx]
                pos = transformation[0:3, 3]
                orn = np.array(quatXYZWFromRotMat(transformation[0:3, 0:3]))
                dynamics_info = p.getDynamicsInfo(body_id, -1)
                inertial_pos, inertial_orn = dynamics_info[3], dynamics_info[4]
                pos, orn = p.multiplyTransforms(
                    pos, orn, inertial_pos, inertial_orn)
                pos = list(pos)
                min_distance_to_existing_object = None
                for existing_object in existing_objects:
                    distance = np.linalg.norm(
                        np.array(pos) -
                        np.array(existing_object.get_position()))
                    if min_distance_to_existing_object is None or \
                       min_distance_to_existing_object > distance:
                        min_distance_to_existing_object = distance

                if min_distance_to_existing_object < min_distance:
                    add = False
                    break

                pos[2] += 0.01  # slighly above to not touch furniture
                p.resetBasePositionAndOrientation(body_id, pos, orn)

            # Filter based on collisions with any existing object
            if add:
                p.stepSimulation()

                for body_id in body_ids:
                    in_collision = len(p.getContactPoints(body_id)) > 0
                    if in_collision:
                        add = False
                        break

            if add:
                objects_to_add.append(obj)

            for body_id in body_ids:
                p.removeBody(body_id)

            p.restoreState(state_id)

        p.removeState(state_id)

        for obj in objects_to_add:
            self.import_object(obj)

    @load_without_pybullet_vis
    def import_robot(self,
                     robot,
                     class_id=SemanticClass.ROBOTS):
        """
        Import a robot into the simulator

        :param robot: Robot
        :param class_id: Class id for rendering semantic segmentation
        :return: pybullet id
        """
        assert isinstance(robot, BaseRobot), \
            'import_robot can only be called with BaseRobot'
        ids = robot.load()
        visual_objects = []
        link_ids = []
        poses_rot = []
        poses_trans = []
        self.robots.append(robot)

        for shape in p.getVisualShapeData(ids[0]):
            id, link_id, type, dimensions, filename, rel_pos, rel_orn, color = shape[:8]
            if type == p.GEOM_MESH:
                filename = filename.decode('utf-8')
                if (filename, tuple(dimensions), tuple(rel_pos), tuple(rel_orn)) not in self.visual_objects.keys():
                    self.renderer.load_object(filename,
                                              transform_orn=rel_orn,
                                              transform_pos=rel_pos,
                                              input_kd=color[:3],
                                              scale=np.array(dimensions))
                    self.visual_objects[(filename, tuple(dimensions), tuple(rel_pos), tuple(rel_orn))
                                        ] = len(self.renderer.visual_objects) - 1
                visual_objects.append(
                    self.visual_objects[(filename, tuple(dimensions), tuple(rel_pos), tuple(rel_orn))])
                link_ids.append(link_id)
            elif type == p.GEOM_SPHERE:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/sphere8.obj')
                self.renderer.load_object(
                    filename,
                    transform_orn=rel_orn,
                    transform_pos=rel_pos,
                    input_kd=color[:3],
                    scale=[dimensions[0] / 0.5, dimensions[0] / 0.5, dimensions[0] / 0.5])
                visual_objects.append(
                    len(self.renderer.get_visual_objects()) - 1)
                link_ids.append(link_id)
            elif type == p.GEOM_CAPSULE or type == p.GEOM_CYLINDER:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/cube.obj')
                self.renderer.load_object(
                    filename,
                    transform_orn=rel_orn,
                    transform_pos=rel_pos,
                    input_kd=color[:3],
                    scale=[dimensions[1] / 0.5, dimensions[1] / 0.5, dimensions[0]])
                visual_objects.append(
                    len(self.renderer.get_visual_objects()) - 1)
                link_ids.append(link_id)
            elif type == p.GEOM_BOX:
                filename = os.path.join(
                    gibson2.assets_path, 'models/mjcf_primitives/cube.obj')
                self.renderer.load_object(filename,
                                          transform_orn=rel_orn,
                                          transform_pos=rel_pos,
                                          input_kd=color[:3],
                                          scale=np.array(dimensions))
                visual_objects.append(
                    len(self.renderer.get_visual_objects()) - 1)
                link_ids.append(link_id)

            if link_id == -1:
                pos, orn = p.getBasePositionAndOrientation(id)
            else:
                _, _, _, _, pos, orn = p.getLinkState(id, link_id)
            poses_rot.append(np.ascontiguousarray(quat2rotmat(xyzw2wxyz(orn))))
            poses_trans.append(np.ascontiguousarray(xyz2mat(pos)))

        self.renderer.add_robot(object_ids=visual_objects,
                                link_ids=link_ids,
                                pybullet_uuid=ids[0],
                                class_id=class_id,
                                poses_rot=poses_rot,
                                poses_trans=poses_trans,
                                dynamic=True,
                                robot=robot)

        return ids

    def add_normal_text(self,
                 text_data='PLACEHOLDER: PLEASE REPLACE!',
                 font_name='OpenSans',
                 font_style='Regular',
                 font_size=48,
                 color=[0, 0, 0],
                 pos=[0, 100],
                 size=[20, 20],
                 scale=1.0,
                 background_color=None):
        """
        Creates a Text object to be rendered to a non-VR screen. Returns the text object to the caller,
        so various settings can be changed - eg. text content, position, scale, etc.
        :param text_data: starting text to display (can be changed at a later time by set_text)
        :param font_name: name of font to render - same as font folder in iGibson assets
        :param font_style: style of font - one of [regular, italic, bold]
        :param font_size: size of font to render
        :param color: [r, g, b] color
        :param pos: [x, y] position of top-left corner of text box, in percentage across screen
        :param size: [w, h] size of text box in percentage across screen-space axes
        :param scale: scale factor for resizing text
        :param background_color: color of the background in form [r, g, b, a] - background will only appear if this is not None
        """
        # Note: For pos/size - (0,0) is bottom-left and (100, 100) is top-right
        # Calculate pixel positions for text
        pixel_pos = [int(pos[0]/100.0 * self.renderer.width), int(pos[1]/100.0 * self.renderer.height)]
        pixel_size = [int(size[0]/100.0 * self.renderer.width), int(size[1]/100.0 * self.renderer.height)]
        return self.renderer.add_text(text_data=text_data,
                                      font_name=font_name,
                                      font_style=font_style,
                                      font_size=font_size,
                                      color=color,
                                      pixel_pos=pixel_pos,
                                      pixel_size=pixel_size,
                                      scale=scale,
                                      background_color=background_color,
                                      render_to_tex=False)

    def add_vr_overlay_text(self,
                 text_data='PLACEHOLDER: PLEASE REPLACE!',
                 font_name='OpenSans',
                 font_style='Regular',
                 font_size=48,
                 color=[0, 0, 0],
                 pos=[20, 80],
                 size=[70, 80],
                 scale=1.0,
                 background_color=[1,1,1,0.8]):
        """
        Creates Text for use in a VR overlay. Returns the text object to the caller,
        so various settings can be changed - eg. text content, position, scale, etc.
        :param text_data: starting text to display (can be changed at a later time by set_text)
        :param font_name: name of font to render - same as font folder in iGibson assets
        :param font_style: style of font - one of [regular, italic, bold]
        :param font_size: size of font to render
        :param color: [r, g, b] color
        :param pos: [x, y] position of top-left corner of text box, in percentage across screen
        :param size: [w, h] size of text box in percentage across screen-space axes
        :param scale: scale factor for resizing text
        :param background_color: color of the background in form [r, g, b, a] - default is semi-transparent white so text is easy to read in VR
        """
        if not self.can_access_vr_context:
            raise RuntimeError('ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')
        if not self.vr_overlay_initialized:
            # This function automatically creates a VR text overlay the first time text is added
            self.renderer.gen_vr_hud()
            self.vr_overlay_initialized = True

        # Note: For pos/size - (0,0) is bottom-left and (100, 100) is top-right
        # Calculate pixel positions for text
        pixel_pos = [int(pos[0]/100.0 * self.renderer.width), int(pos[1]/100.0 * self.renderer.height)]
        pixel_size = [int(size[0]/100.0 * self.renderer.width), int(size[1]/100.0 * self.renderer.height)]
        return self.renderer.add_text(text_data=text_data,
                                      font_name=font_name,
                                      font_style=font_style,
                                      font_size=font_size,
                                      color=color,
                                      pixel_pos=pixel_pos,
                                      pixel_size=pixel_size,
                                      scale=scale,
                                      background_color=background_color,
                                      render_to_tex=True)

    def add_overlay_image(self,
                        image_fpath,
                        width=1,
                        pos=[0,0,-1]):
        """
        Add an image with a given file path to the VR overlay. This image will be displayed
        in addition to any text that the users wishes to display. This function returns a handle
        to the VrStaticImageOverlay, so the user can display/hide it at will.
        """
        if not self.can_access_vr_context:
            raise RuntimeError('ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')
        return self.renderer.gen_static_overlay(image_fpath, width=width, pos=pos)

    def set_hud_show_state(self, show_state):
        """
        Shows/hides the main VR HUD.
        :param show_state: whether to show HUD or not
        """
        if not self.can_access_vr_context:
            raise RuntimeError('ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')
        self.renderer.vr_hud.set_overlay_show_state(show_state)

    def get_hud_show_state(self):
        """
        Returns the show state of the main VR HUD.
        """
        if not self.can_access_vr_context:
            raise RuntimeError('ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')
        return self.renderer.vr_hud.get_overlay_show_state()

    def _non_physics_step(self):
        """
        Complete any non-physics steps such as state updates.
        """
        # Step all of the particle systems.
        for particle_system in self.particle_systems:
            particle_system.update(self)

        # Step the object states in global topological order.
        for state_type in self.object_state_types:
            for obj in self.scene.get_objects_with_state(state_type):
                obj.states[state_type].update(self)

    def step_vr(self, print_stats=False):
        """
        Step the simulation when using VR. Order of function calls:
        1) Simulate physics
        2) Render frame
        3) Submit rendered frame to VR compositor
        4) Update VR data for use in the next frame
        """
        assert self.scene is not None, \
            "A scene must be imported before running the simulator. Use EmptyScene for an empty scene."

        # Calculate time outside of step
        outside_step_dur = 0
        if self.frame_end_time is not None:
            outside_step_dur = time.perf_counter() - self.frame_end_time
        # Simulate Physics in PyBullet
        physics_start_time = time.perf_counter()
        physics_timestep_num = self.num_phys_steps
        for _ in range(physics_timestep_num):
            p.stepSimulation()
        self._non_physics_step()
        physics_dur = time.perf_counter() - physics_start_time

        # Sync PyBullet bodies to renderer and then render to Viewer
        render_start_time = time.perf_counter()
        self.sync()
        render_dur = time.perf_counter() - render_start_time

        # Update VR compositor and VR data
        vr_system_start = time.perf_counter()
        # First sync VR compositor - this is where Oculus blocks (as opposed to Vive, which blocks in update_vr_data)
        self.sync_vr_compositor()
        # Note: this should only be called once per frame - use get_vr_events to read the event data list in
        # subsequent read operations
        self.poll_vr_events()
        # This is necessary to fix the eye tracking value for the current frame, since it is multi-threaded
        self.fix_eye_tracking_value()
        # Move user to their starting location
        self.perform_vr_start_pos_move()
        # Update VR data and wait until 3ms before the next vsync
        self.renderer.update_vr_data()
        vr_system_dur = time.perf_counter() - vr_system_start

        # Sleep until we reach the last frame before desired vsync point
        phys_rend_dur = outside_step_dur + physics_dur + render_dur + vr_system_dur
        sleep_start_time = time.perf_counter()
        if phys_rend_dur < self.fixed_frame_dur:
            sleep(self.fixed_frame_dur - phys_rend_dur)
        sleep_dur = time.perf_counter() - sleep_start_time

        # Calculate final frame duration
        # Make sure it is non-zero for FPS calculation (set to max of 1000 if so)
        frame_dur = max(1e-3, phys_rend_dur + sleep_dur)

        # Set variables for data saving and replay
        self.last_physics_timestep = physics_dur
        self.last_render_timestep = render_dur
        self.last_physics_step_num = physics_timestep_num
        self.last_frame_dur = frame_dur

        if print_stats:
            print('Frame number {} statistics (ms)'.format(self.frame_count))
            print('Total out-of-step duration: {}'.format(outside_step_dur * 1000))
            print('Total physics duration: {}'.format(physics_dur * 1000))
            print('Total render duration: {}'.format(render_dur * 1000))
            print('Total sleep duration: {}'.format(sleep_dur * 1000))
            print('Total VR system duration: {}'.format(vr_system_dur * 1000))
            print('Total frame duration: {} and fps: {}'.format(
                frame_dur * 1000, 1/frame_dur))
            print('Realtime factor: {}'.format(
                round(physics_timestep_num * self.physics_timestep / frame_dur, 3)))
            print('-------------------------')

        self.frame_count += 1
        self.frame_end_time = time.perf_counter()

    def step_block_test(self, sleep_time):
        """
        Function that sleeps and renders simple scene to VR, to figure
        out relationship between frame time and VR blocking time.
        """
        non_vr_start = time.perf_counter()
        # Takes less than 3ms
        render_start_time = time.perf_counter()
        for _ in range(1):
            p.stepSimulation()
        self.sync()
        render_dur = time.perf_counter() - render_start_time

        # Sleep for remainder of frame
        # First frame is invalid, so return None
        if sleep_time < render_dur:
            return (None, None)

        time.sleep(sleep_time - render_dur)
        non_vr_dur = time.perf_counter() - non_vr_start

        # Do VR system stuff
        vr_system_start = time.perf_counter()
        self.sync_vr_compositor()
        self.poll_vr_events()
        self.fix_eye_tracking_value()
        self.perform_vr_start_pos_move()
        self.renderer.update_vr_data()
        vr_system_dur = time.perf_counter() - vr_system_start

        # Return Vr system duration to user, as well as non-vr frame time
        # Values are in ms
        return (vr_system_dur * 1000, non_vr_dur * 1000)

    def step(self, print_stats=False, forced_timestep=None):
        """
        Step the simulation at self.render_timestep and update positions in renderer
        """
        # Call separate step function for VR
        if self.can_access_vr_context:
            self.step_vr(print_stats=print_stats)
            return

        # Always guarantee at least one physics timestep
        physics_timestep_num = forced_timestep if forced_timestep else max(
            1, int(self.render_timestep / self.physics_timestep))
        for _ in range(physics_timestep_num):
            p.stepSimulation()
        self._non_physics_step()
        self.sync()

    def sync(self):
        """
        Update positions in renderer without stepping the simulation. Usually used in the reset() function
        """
        self.body_links_awake = 0
        for instance in self.renderer.instances:
            if instance.dynamic:
                self.body_links_awake += self.update_position(instance)
        if (self.use_ig_renderer or self.use_vr_renderer or self.use_simple_viewer) and self.viewer is not None:
            self.viewer.update()
        if self.first_sync:
            self.first_sync = False

    def sync_vr_compositor(self):
        """
        Sync VR compositor.
        """
        self.renderer.vr_compositor_update()

    def perform_vr_start_pos_move(self):
        """
        Sets the VR position on the first step iteration where the hmd tracking is valid. Not to be confused
        with self.set_vr_start_pos, which simply records the desired start position before the simulator starts running.
        """
        # Update VR start position if it is not None and the hmd is valid
        # This will keep checking until we can successfully set the start position
        if self.vr_start_pos:
            hmd_is_valid, _, _, _ = self.renderer.vrsys.getDataForVRDevice(
                'hmd')
            if hmd_is_valid:
                offset_to_start = np.array(
                    self.vr_start_pos) - self.get_hmd_world_pos()
                if self.vr_height_offset is not None:
                    offset_to_start[2] = self.vr_height_offset
                self.set_vr_offset(offset_to_start)
                self.vr_start_pos = None

    def fix_eye_tracking_value(self):
        """
        Calculates and fixes eye tracking data to its value during step(). This is necessary, since multiple
        calls to get eye tracking data return different results, due to the SRAnipal multithreaded loop that
        runs in parallel to the iGibson main thread
        """
        self.eye_tracking_data = self.renderer.vrsys.getEyeTrackingData()

    def gen_assisted_grasping_categories(self):
        """
        Generates list of categories that can be grasped using assisted grasping,
        using labels provided in average category specs file.
        """
        avg_category_spec = get_ig_avg_category_specs()
        for k, v in avg_category_spec.items():
            if v['enable_ag']:
                self.assist_grasp_category_allow_list.append(k)

    def can_assisted_grasp(self, body_id, c_link):
        """
        Checks to see if an object with the given body_id can be grasped. This is done
        by checking its category to see if is in the allowlist.
        """
        if body_id not in self.scene.objects_by_id or self.scene.objects_by_id[body_id].category == 'object':
            mass = p.getDynamicsInfo(body_id, c_link)[0]
            return mass <= self.vr_settings.assist_grasp_mass_thresh
        else:
            return self.scene.objects_by_id[body_id].category in self.assist_grasp_category_allow_list

    def poll_vr_events(self):
        """
        Returns VR event data as list of lists. 
        List is empty if all events are invalid. Components of a single event:
        controller: 0 (left_controller), 1 (right_controller)
        button_idx: any valid idx in EVRButtonId enum in openvr.h header file
        press: 0 (unpress), 1 (press)
        """
        if not self.can_access_vr_context:
            raise RuntimeError(
                'ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')

        self.vr_event_data = self.renderer.vrsys.pollVREvents()
        # Enforce store_first_button_press_per_frame option, if user has enabled it
        if self.vr_settings.store_only_first_event_per_button:
            temp_event_data = []
            # Make sure we only store the first (button, press) combo of each type
            event_set = set()
            for ev_data in self.vr_event_data:
                controller, button_idx, _ = ev_data
                key = (controller, button_idx)
                if key not in event_set:
                    temp_event_data.append(ev_data)
                    event_set.add(key)
            self.vr_event_data = temp_event_data[:]
        
        return self.vr_event_data

    def get_vr_events(self):
        """
        Returns the VR events processed by the simulator
        """
        return self.vr_event_data

    def query_vr_event(self, controller, action):
        """
        Queries system for a VR event, and returns true if that event happened this frame
        :param controller: device to query for - can be left_controller or right_controller
        :param action: an action name listed in "action_button_map" dictionary for the current device in the vr_config.json
        """
        # Return false if any of input parameters are invalid
        if (controller not in ['left_controller', 'right_controller'] or 
            action not in self.vr_settings.action_button_map.keys()):
            return False

        # Search through event list to try to find desired event
        controller_id = 0 if controller == 'left_controller' else 1
        button_idx, press_id = self.vr_settings.action_button_map[action]
        for ev_data in self.vr_event_data:
            if controller_id == ev_data[0] and button_idx == ev_data[1] and press_id == ev_data[2]:
                return True

        # Return false if event was not found this frame
        return False

    def get_data_for_vr_device(self, device_name):
        """
        Call this after step - returns all VR device data for a specific device
        Returns is_valid (indicating validity of data), translation and rotation in Gibson world space
        :param device_name: can be hmd, left_controller or right_controller
        """
        if not self.can_access_vr_context:
            raise RuntimeError(
                'ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')

        # Use fourth variable in list to get actual hmd position in space
        is_valid, translation, rotation, _ = self.renderer.vrsys.getDataForVRDevice(device_name)
        return [is_valid, translation, rotation]

    def get_hmd_world_pos(self):
        """
        Get world position of HMD without offset
        """
        if not self.can_access_vr_context:
            raise RuntimeError(
                'ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')

        _, _, _, hmd_world_pos = self.renderer.vrsys.getDataForVRDevice('hmd')
        return hmd_world_pos

    def get_button_data_for_controller(self, controller_name):
        """
        Call this after getDataForVRDevice - returns analog data for a specific controller
        Returns trigger_fraction, touchpad finger position x, touchpad finger position y
        Data is only valid if isValid is true from previous call to getDataForVRDevice
        Trigger data: 1 (closed) <------> 0 (open)
        Analog data: X: -1 (left) <-----> 1 (right) and Y: -1 (bottom) <------> 1 (top)
        :param controller_name: one of left_controller or right_controller
        """
        if not self.can_access_vr_context:
            raise RuntimeError('ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')
        
        trigger_fraction, touch_x, touch_y = self.renderer.vrsys.getButtonDataForController(controller_name)
        return [trigger_fraction, touch_x, touch_y]

    def get_scroll_input(self):
        """
        Gets scroll input. This uses the non-movement-controller, and determines whether
        the user wants to scroll by testing if they have pressed the touchpad, while keeping
        their finger on the top/button of the pad. Return True for up and False for down (-1 for no scroll)
        """
        mov_controller = self.vr_settings.movement_controller
        other_controller = 'right' if mov_controller == 'left' else 'left'
        other_controller = '{}_controller'.format(other_controller)
        # Data indicating whether user has pressed top or bottom of the touchpad
        _, _, touch_y = self.renderer.vrsys.getButtonDataForController(other_controller)
        # Detect no touch in extreme regions of y axis
        if touch_y > 0.7 and touch_y <= 1.0:
            return 1
        elif touch_y < -0.7 and touch_y >= -1.0:
            return 0
        else:
            return -1
    
    def get_eye_tracking_data(self):
        """
        Returns eye tracking data as list of lists. Order: is_valid, gaze origin, gaze direction, gaze point, 
        left pupil diameter, right pupil diameter (both in millimeters)
        Call after getDataForVRDevice, to guarantee that latest HMD transform has been acquired
        """
        if self.eye_tracking_data is None:
            return [0, [0,0,0], [0,0,0], 0, 0]
        is_valid, origin, dir, left_pupil_diameter, right_pupil_diameter = self.eye_tracking_data
        return [is_valid, origin, dir, left_pupil_diameter, right_pupil_diameter]

    def set_vr_start_pos(self, start_pos=None, vr_height_offset=None):
        """
        Sets the starting position of the VR system in iGibson space
        :param start_pos: position to start VR system at
        :param vr_height_offset: starting height offset. If None, uses absolute height from start_pos
        """
        if not self.can_access_vr_context:
            raise RuntimeError(
                'ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')

        # The VR headset will actually be set to this position during the first frame.
        # This is because we need to know where the headset is in space when it is first picked
        # up to set the initial offset correctly.
        self.vr_start_pos = start_pos
        # This value can be set to specify a height offset instead of an absolute height.
        # We might want to adjust the height of the camera based on the height of the person using VR,
        # but still offset this height. When this option is not None it offsets the height by the amount
        # specified instead of overwriting the VR system height output.
        self.vr_height_offset = vr_height_offset

    def set_vr_pos(self, pos=None):
        """
        Sets the world position of the VR system in iGibson space
        :param pos: position to set VR system to
        """
        if not self.can_access_vr_context:
            raise RuntimeError(
                'ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')

        offset_to_pos = np.array(pos) - self.get_hmd_world_pos()
        self.set_vr_offset(offset_to_pos)

    def get_vr_pos(self):
        """
        Gets the world position of the VR system in iGibson space.
        """
        return self.get_hmd_world_pos() + self.get_vr_offset()

    def set_vr_offset(self, pos=None):
        """
        Sets the translational offset of the VR system (HMD, left controller, right controller) from world space coordinates.
        Can be used for many things, including adjusting height and teleportation-based movement
        :param pos: must be a list of three floats, corresponding to x, y, z in Gibson coordinate space
        """
        if not self.can_access_vr_context:
            raise RuntimeError(
                'ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')

        self.renderer.vrsys.setVROffset(-pos[1], pos[2], -pos[0])

    def get_vr_offset(self):
        """
        Gets the current VR offset vector in list form: x, y, z (in iGibson coordinates)
        """
        if not self.can_access_vr_context:
            raise RuntimeError(
                'ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')

        x, y, z = self.renderer.vrsys.getVROffset()
        return [x, y, z]

    def get_device_coordinate_system(self, device):
        """
        Gets the direction vectors representing the device's coordinate system in list form: x, y, z (in Gibson coordinates)
        List contains "right", "up" and "forward" vectors in that order
        :param device: can be one of "hmd", "left_controller" or "right_controller"
        """
        if not self.can_access_vr_context:
            raise RuntimeError(
                'ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')

        vec_list = []

        coordinate_sys = self.renderer.vrsys.getDeviceCoordinateSystem(device)
        for dir_vec in coordinate_sys:
            vec_list.append(dir_vec)

        return vec_list

    def trigger_haptic_pulse(self, device, strength):
        """
        Triggers a haptic pulse of the specified strength (0 is weakest, 1 is strongest)
        :param device: device to trigger haptic for - can be any one of [left_controller, right_controller]
        :param strength: strength of haptic pulse (0 is weakest, 1 is strongest)
        """
        if not self.can_access_vr_context:
            raise RuntimeError('ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')
        assert device in ['left_controller', 'right_controller']
      
        self.renderer.vrsys.triggerHapticPulseForDevice(device, int(self.max_haptic_duration * strength))

    def set_hidden_state(self, obj, hide=True):
        """
        Sets the hidden state of an object to be either hidden or not hidden.
        The object passed in must inherent from Object at the top level

        Note: this function must be called after step() in the rendering loop
        Note 2: this function only works with the optimized renderer - please use the renderer hidden
        list to hide objects in the non-optimized renderer
        """
        # Find instance corresponding to this id in the renderer
        for instance in self.renderer.instances:
            if obj.body_id == instance.pybullet_uuid:
                instance.hidden = hide
                self.renderer.update_hidden_state([instance])
                return

    def set_hud_state(self, state):
        """
        Sets state of the VR HUD (heads-up-display)
        :param state: one of 'show' or 'hide'
        """
        if not self.can_access_vr_context:
            raise RuntimeError('ERROR: Trying to access VR context without enabling vr mode and use_vr in vr settings!')
        if self.renderer.vr_hud:
            self.renderer.vr_hud.set_overlay_state(state)

    def get_hidden_state(self, obj):
        """
        Returns the current hidden state of the object - hidden (True) or not hidden (False)
        """
        for instance in self.renderer.instances:
            if obj.body_id == instance.pybullet_uuid:
                return instance.hidden

    def get_category_ids(self, category_name):
        """
        Gets ids for all instances of a specific category (floors, walls, etc.) in a scene
        """
        if not hasattr(self.scene, 'objects_by_id'):
            return []
        return [body_id for body_id in self.objects if body_id in self.scene.objects_by_id.keys() and self.scene.objects_by_id[body_id].category == category_name]

    def update_position(self, instance):
        """
        Update position for an object or a robot in renderer.
        :param instance: Instance in the renderer
        """
        body_links_awake = 0
        if isinstance(instance, Instance):
            dynamics_info = p.getDynamicsInfo(instance.pybullet_uuid, -1)
            inertial_pos = dynamics_info[3]
            inertial_orn = dynamics_info[4]
            if len(dynamics_info) == 13 and not self.first_sync:
                activation_state = dynamics_info[12]
            else:
                activation_state = PyBulletSleepState.AWAKE

            if activation_state != PyBulletSleepState.AWAKE:
                return body_links_awake
            # pos and orn of the inertial frame of the base link,
            # instead of the base link frame
            pos, orn = p.getBasePositionAndOrientation(
                instance.pybullet_uuid)

            # Need to convert to the base link frame because that is
            # what our own renderer keeps track of
            # Based on pyullet docuementation:
            # urdfLinkFrame = comLinkFrame * localInertialFrame.inverse().

            inv_inertial_pos, inv_inertial_orn =\
                p.invertTransform(inertial_pos, inertial_orn)
            # Now pos and orn are converted to the base link frame
            pos, orn = p.multiplyTransforms(
                pos, orn, inv_inertial_pos, inv_inertial_orn)

            instance.set_position(pos)
            instance.set_rotation(quat2rotmat(xyzw2wxyz(orn)))
            body_links_awake += 1
        elif isinstance(instance, InstanceGroup):
            for j, link_id in enumerate(instance.link_ids):
                if link_id == -1:
                    dynamics_info = p.getDynamicsInfo(
                        instance.pybullet_uuid, -1)
                    inertial_pos = dynamics_info[3]
                    inertial_orn = dynamics_info[4]
                    if len(dynamics_info) == 13 and not self.first_sync:
                        activation_state = dynamics_info[12]
                    else:
                        activation_state = PyBulletSleepState.AWAKE

                    if activation_state != PyBulletSleepState.AWAKE:
                        continue
                    # same conversion is needed as above
                    pos, orn = p.getBasePositionAndOrientation(
                        instance.pybullet_uuid)

                    inv_inertial_pos, inv_inertial_orn =\
                        p.invertTransform(inertial_pos, inertial_orn)
                    pos, orn = p.multiplyTransforms(
                        pos, orn, inv_inertial_pos, inv_inertial_orn)
                else:
                    dynamics_info = p.getDynamicsInfo(
                        instance.pybullet_uuid, link_id)

                    if len(dynamics_info) == 13 and not self.first_sync:
                        activation_state = dynamics_info[12]
                    else:
                        activation_state = PyBulletSleepState.AWAKE

                    if activation_state != PyBulletSleepState.AWAKE:
                        continue
                    _, _, _, _, pos, orn = p.getLinkState(
                        instance.pybullet_uuid, link_id)

                instance.set_position_for_part(xyz2mat(pos), j)
                instance.set_rotation_for_part(
                    quat2rotmat(xyzw2wxyz(orn)), j)
                body_links_awake += 1
        return body_links_awake

    def isconnected(self):
        """
        :return: pybullet is alive
        """
        return p.getConnectionInfo(self.cid)['isConnected']

    def disconnect(self):
        """
        Clean up the simulator
        """
        if self.isconnected():
            # print("******************PyBullet Logging Information:")
            p.resetSimulation(physicsClientId=self.cid)
            p.disconnect(self.cid)
            # print("PyBullet Logging Information******************")
        self.renderer.release()

    def disconnect_pybullet(self):
        """
        Disconnects only pybullet - used for multi-user VR
        """
        if self.isconnected():
            p.resetSimulation(physicsClientId=self.cid)
            p.disconnect(self.cid)
