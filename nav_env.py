import time

import numpy as np
from spot_wrapper.spot import Spot
from spot_wrapper.utils import say

from base_env import SpotBaseEnv
from real_policy import NavPolicy
from utils import construct_config, get_default_parser, nav_target_from_waypoints


def main(spot):
    parser = get_default_parser()
    parser.add_argument("-g", "--goal")
    parser.add_argument("-w", "--waypoint")
    parser.add_argument("-d", "--dock", action="store_true")
    args = parser.parse_args()
    config = construct_config(args.opts)

    policy = NavPolicy(config.WEIGHTS.NAV, device=config.DEVICE)
    policy.reset()

    env = SpotNavEnv(config, spot)
    if args.waypoint is not None:
        goal_x, goal_y, goal_heading = nav_target_from_waypoints(args.waypoint)
    else:
        assert args.goal is not None
        goal_x, goal_y, goal_heading = [float(i) for i in args.goal.split(",")]
    observations = env.reset((goal_x, goal_y), goal_heading)
    done = False
    say("Starting episode")
    time.sleep(2)
    try:
        while not done:
            action = policy.act(observations)
            observations, _, done, _ = env.step(base_action=action)
        say("Environment is done.")
        if args.dock:
            say("Docking robot")
            spot.dock(dock_id=520, home_robot=True)
    finally:
        spot.power_off()


class SpotNavEnv(SpotBaseEnv):
    def __init__(self, config, spot: Spot):
        super().__init__(config, spot)
        self.goal_xy = None
        self.goal_heading = None
        self.succ_distance = config.SUCCESS_DISTANCE
        self.succ_angle = config.SUCCESS_ANGLE_DIST

    def reset(self, goal_xy, goal_heading):
        self.goal_xy = np.array(goal_xy, dtype=np.float32)
        self.goal_heading = goal_heading
        observations = super().reset()
        assert len(self.goal_xy) == 2

        return observations

    def get_success(self, observations):
        succ = self.get_nav_success(observations, self.succ_distance, self.succ_angle)
        if succ:
            self.spot.set_base_velocity(0.0, 0.0, 0.0, 1 / self.ctrl_hz)
        return succ

    def get_observations(self):
        return self.get_nav_observation(self.goal_xy, self.goal_heading)


if __name__ == "__main__":
    spot = Spot("RealNavEnv")
    with spot.get_lease(hijack=True):
        main(spot)
