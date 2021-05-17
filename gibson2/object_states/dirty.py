from gibson2.object_states import AABB
from gibson2.object_states.object_state_base import AbsoluteObjectState
from gibson2.object_states.object_state_base import BooleanState
from gibson2.objects.particles import Dust, Stain

CLEAN_THRESHOLD = 0.5
MIN_PARTICLES_FOR_SAMPLING_SUCCESS = 5


class _Dirty(AbsoluteObjectState, BooleanState):
    """
    This class represents common logic between particle-based dirtyness states like
    dusty and stained. It should not be directly instantiated - use subclasses instead.
    """
    @staticmethod
    def get_dependencies():
        return AbsoluteObjectState.get_dependencies() + [AABB]

    def __init__(self, obj):
        super(_Dirty, self).__init__(obj)
        self.value = False
        self.dirt = None

        # Keep dump data for when we initialize our dirt.
        self.from_dump = None

    def _initialize(self, simulator):
        self.dirt = self.DIRT_CLASS(self.obj, from_dump=self.from_dump)
        simulator.import_particle_system(self.dirt)

    def _get_value(self):
        max_particles_for_clean = (
                self.dirt.get_num_particles_activated_at_any_time() * CLEAN_THRESHOLD)
        return self.dirt.get_num_active() > max_particles_for_clean

    def _set_value(self, new_value):
        self.value = new_value
        if not self.value:
            for particle in self.dirt.get_active_particles():
                self.dirt.stash_particle(particle)
        else:
            self.dirt.randomize()

            # If after randomization we have too few particles, stash them and return False.
            if self.dirt.get_num_particles_activated_at_any_time() < MIN_PARTICLES_FOR_SAMPLING_SUCCESS:
                for particle in self.dirt.get_active_particles():
                    self.dirt.stash_particle(particle)

                return False

        return True

    def _dump(self):
        return {
            "value": self.value,
            "particles": self.dirt.dump(),
        }

    def _load(self, data):
        self.value = data["value"]
        self.from_dump = data["particles"]


class Dusty(_Dirty):
    DIRT_CLASS = Dust


class Stained(_Dirty):
    DIRT_CLASS = Stain
