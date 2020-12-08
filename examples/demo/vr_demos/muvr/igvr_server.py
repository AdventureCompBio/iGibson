"""Server code that handles physics simulation and communicates with remote IGVRClient objects."""


import numpy as np
import time

from gibson2.render.mesh_renderer.mesh_renderer_cpu import Instance, InstanceGroup

from PodSixNet.Channel import Channel
from PodSixNet.Server import Server


class IGVRChannel(Channel):
    """
    This is a server representation of a single, connected IGVR client.
    """
    def __init__(self, *args, **kwargs):
        Channel.__init__(self, *args, **kwargs)
    
    def Close(self):
        print(self, "Client disconnected")
        
    def Network_vrdata(self, data):
        self.vr_data_cb(data["vr_data"])

    def set_vr_data_callback(self, cb):
        """
        Sets callback to be called when vr data is received. In this case,
        we call a function on the server to update the positions/constraints of all the
        VR objects in the scene.

        Note: cb should take in one parameter - vr_data
        """
        self.vr_data_cb = cb
        
    def send_frame_data(self, frame_data):
        """
        Sends frame data to an IGVRClient.
        """
        self.Send({"action":"syncframe", "frame_data":frame_data})
    

class IGVRServer(Server):
    """
    This is the IGVR server that handles communication with remote clients.
    """
    # Define channel class for the server
    channelClass = IGVRChannel
    
    def __init__(self, *args, **kwargs):
        """
        Initializes the IGVRServer.
        """
        Server.__init__(self, *args, **kwargs)
        print('IGVR server launched!')
        # This server manages a single vr client
        self.vr_client = None
        self.last_comm_time = time.time()

    def has_client(self):
        """
        Returns whether the server has a client connected.
        """
        return self.vr_client is not None

    def register_data(self, sim, client_agent):
        """
        Register the simulator and renderer and VrAgent objects from which the server will collect frame data
        """
        self.s = sim
        self.renderer = sim.renderer
        self.client_agent = client_agent

    def update_client_vr_data(self, vr_data):
        """
        Updates VR objects based on data sent by client. This function is called from the asynchronous
        Network_vrdata that is first called by the client channel.
        """
        time_since_last_comm = time.time() - self.last_comm_time
        self.last_comm_time = time.time()
        #print("Time since last comm: {}".format(time_since_last_comm))
        #print("Comm fps: {}".format(1/time_since_last_comm))
        # Only update if there is data to read - when the client is in non-vr mode, it sends empty lists
        if vr_data:
            # Delegate manual updates to the VrAgent class
            #self.client_agent.update(vr_data)
            gm = self.client_agent.vr_dict['gaze_marker']
            eye_dat = vr_data['eye_data']

            is_eye_data_valid, origin, dir, left_pupil_diameter, right_pupil_diameter = eye_dat
            if is_eye_data_valid:
                print("Old positoin: {}".format(gm.get_position()))
                updated_marker_pos = [origin[0] + dir[0], origin[1] + dir[1], origin[2] + dir[2]]
                print("New position: {}".format(updated_marker_pos))
                gm.set_position(updated_marker_pos)
                print("----- Eye data is valid! -----")

            """
            print("Current client position in server:")
            print(self.client_agent.vr_dict['body'].get_position())
            print(self.client_agent.vr_dict['left_hand'].get_position())
            print(self.client_agent.vr_dict['right_hand'].get_position())
            print(self.client_agent.vr_dict['gaze_marker'].get_position())
            print("Eye data: {}".format(vr_data['eye_data']))
            """
    
    def Connected(self, channel, addr):
        """
        Called each time a new client connects to the server.
        """
        print("New connection:", channel)
        self.vr_client = channel
        self.vr_client.set_vr_data_callback(self.update_client_vr_data)
        
    def generate_frame_data(self):
        """
        Generates frame data to send to client
        """
        # Frame data is stored as a dictionary mapping pybullet uuid to pose/rot data
        frame_data = {}
        # It is assumed that the client renderer will have loaded instances in the same order as the server
        for instance in self.renderer.get_instances():
            # Loop through all instances and get pos and rot data
            # We convert numpy arrays into lists so they can be serialized and sent over the network
            # Lists can also be easily reconstructed back into numpy arrays on the client side
            if isinstance(instance, Instance):
                pose = instance.pose_trans.tolist()
                rot = instance.pose_rot.tolist()
                frame_data[instance.pybullet_uuid] = [pose, rot]
            elif isinstance(instance, InstanceGroup):
                poses = []
                rots = []
                for pose in instance.poses_trans:
                    poses.append(pose.tolist())
                for rot in instance.poses_rot:
                    rots.append(rot.tolist())

                frame_data[instance.pybullet_uuid] = [poses, rots]

        return frame_data

    def refresh_server(self):
        """
        Pumps the server to refresh incoming/outgoing connections.
        """
        self.Pump()

        if self.vr_client:
            frame_data = self.generate_frame_data()
            self.vr_client.send_frame_data(frame_data)