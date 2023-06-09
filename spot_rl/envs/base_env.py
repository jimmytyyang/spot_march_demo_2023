import os
import os.path as osp
import time

import cv2
import gym

from spot_rl.utils.img_publishers import MAX_HAND_DEPTH
from spot_rl.utils.mask_rcnn_utils import (
    generate_mrcnn_detections,
    get_deblurgan_model,
    get_mrcnn_model,
    pred2string,
)
from spot_rl.utils.robot_subscriber import SpotRobotSubscriberMixin
from spot_rl.utils.stopwatch import Stopwatch

try:
    import magnum as mn
except:
    pass

import numpy as np
import quaternion
import rospy

try:
    from deblur_gan.predictor import DeblurGANv2
    from mask_rcnn_detectron2.inference import MaskRcnnInference
except:
    pass

from sensor_msgs.msg import Image
from spot_wrapper.spot import Spot, wrap_heading
from std_msgs.msg import Float32, String

from spot_rl.utils.utils import FixSizeOrderedDict, arr2str, object_id_to_object_name
from spot_rl.utils.utils import ros_topics as rt

MAX_CMD_DURATION = 5
GRASP_VIS_DIR = osp.join(
    osp.dirname(osp.dirname(osp.abspath(__file__))), "grasp_visualizations"
)
if not osp.isdir(GRASP_VIS_DIR):
    os.mkdir(GRASP_VIS_DIR)

DETECTIONS_BUFFER_LEN = 60
LEFT_CROP = 124
RIGHT_CROP = 60
NEW_WIDTH = 228
NEW_HEIGHT = 240
ORIG_WIDTH = 640
ORIG_HEIGHT = 480
WIDTH_SCALE = 0.5
HEIGHT_SCALE = 0.5

# Detection using owlvit model
from spot_rl.utils.owlvit_utils import OwlVit
# Sequential flag
USE_IMG_RESPONSE = False



def pad_action(action):
    """We only control 4 out of 6 joints; add zeros to non-controllable indices."""
    return np.array([*action[:3], 0.0, action[3], 0.0])


def rescale_actions(actions, action_thresh=0.05, silence_only=False):
    actions = np.clip(actions, -1, 1)
    # Silence low actions
    actions[np.abs(actions) < action_thresh] = 0.0
    if silence_only:
        return actions

    # Remap action scaling to compensate for silenced values
    action_offsets = np.ones_like(actions) * action_thresh
    action_offsets[actions < 0] = -action_offsets[actions < 0]
    action_offsets[actions == 0] = 0
    actions = (actions - np.array(action_offsets)) / (1.0 - action_thresh)

    return actions


class SpotBaseEnv(SpotRobotSubscriberMixin, gym.Env):
    node_name = "spot_reality_gym"
    no_raw = True
    proprioception = True

    def __init__(self, config, spot: Spot, stopwatch=None):
        self.detections_buffer = {
            k: FixSizeOrderedDict(maxlen=DETECTIONS_BUFFER_LEN)
            for k in ["detections", "filtered_depth", "viz"]
        }

        super().__init__(spot=spot)

        self.config = config
        self.spot = spot
        if stopwatch is None:
            stopwatch = Stopwatch()
        self.stopwatch = stopwatch

        # General environment parameters
        self.ctrl_hz = config.CTRL_HZ
        self.max_episode_steps = config.MAX_EPISODE_STEPS
        self.num_steps = 0
        self.reset_ran = False

        # Base action parameters
        self.max_lin_dist = config.MAX_LIN_DIST
        self.max_ang_dist = np.deg2rad(config.MAX_ANG_DIST)

        # Arm action parameters
        self.initial_arm_joint_angles = np.deg2rad(config.INITIAL_ARM_JOINT_ANGLES)
        self.arm_lower_limits = np.deg2rad(config.ARM_LOWER_LIMITS)
        self.arm_upper_limits = np.deg2rad(config.ARM_UPPER_LIMITS)
        self.locked_on_object_count = 0
        self.grasp_attempted = False
        self.place_attempted = False
        self.detection_timestamp = -1

        self.forget_target_object_steps = config.FORGET_TARGET_OBJECT_STEPS
        self.curr_forget_steps = 0
        self.obj_center_pixel = None
        self.target_obj_name = None
        self.last_target_obj = None
        self.use_mrcnn = config.USE_MRCNN
        self.target_object_distance = -1
        self.detections_str_synced = "None"
        self.latest_synchro_obj_detection = None
        self.mrcnn_viz = None
        self.last_seen_objs = []
        self.slowdown_base = -1
        self.prev_base_moved = False
        self.should_end = False

        # Text-to-speech
        self.tts_pub = rospy.Publisher(rt.TEXT_TO_SPEECH, String, queue_size=1)

        # Mask RCNN / Gaze
        self.parallel_inference_mode = config.PARALLEL_INFERENCE_MODE
        if config.PARALLEL_INFERENCE_MODE:
            if config.USE_MRCNN:
                rospy.Subscriber(rt.DETECTIONS_TOPIC, String, self.detections_cb)
                print("Parallel inference selected: Waiting for Mask R-CNN msgs...")
                st = time.time()
                while (
                    len(self.detections_buffer["detections"]) == 0
                    and time.time() < st + 5
                ):
                    pass
                assert (
                    len(self.detections_buffer["detections"]) > 0
                ), "Mask R-CNN msgs not found!"
                print("...msgs received.")
                scale_pub = rospy.Publisher(rt.IMAGE_SCALE, Float32, queue_size=1)
                scale_pub.publish(config.IMAGE_SCALE)
            else: # This is the one that uses owlvit model
                rospy.Subscriber(rt.OWLVIT_DETECTIONS_TOPIC, String, self.detections_cb)
                print("Parallel inference selected: Waiting for OWLVIT msgs...")
                st = time.time()
                while (
                    len(self.detections_buffer["detections"]) == 0
                    and time.time() < st + 5
                ):
                    pass
                assert (
                    len(self.detections_buffer["detections"]) > 0
                ), "OWLVIT msgs not found!"
                print("...msgs received.")
                scale_pub = rospy.Publisher(rt.IMAGE_SCALE, Float32, queue_size=1)
                scale_pub.publish(config.IMAGE_SCALE)
        elif config.USE_MRCNN:
            self.mrcnn = get_mrcnn_model(config)
            self.deblur_gan = get_deblurgan_model(config)
        else:
            self.owlvit = OwlVit([['lion plush', 'penguin plush', 'teddy bear', 'bear plush', 'caterpilar plush', 'ball plush', 'rubiks cube']], 0.1, False)
            self.deblur_gan = get_deblurgan_model(config)

        if config.USE_MRCNN:
            self.mrcnn_viz_pub = rospy.Publisher(
                rt.MASK_RCNN_VIZ_TOPIC, Image, queue_size=1
            )
        else: # This is the one that uses owlvit model
            self.owlvit_viz_pub = rospy.Publisher(
                rt.OWLVIT_VIZ_TOPIC, Image, queue_size=1
            )

        if config.USE_HEAD_CAMERA:
            print("Waiting for filtered depth msgs...")
            st = time.time()
            while self.filtered_head_depth is None and time.time() < st + 15:
                pass
            assert self.filtered_head_depth is not None, "Depth msgs not found!"
            print("...msgs received.")

        self.owlvit_pick_up_object_name = "box"

    @property
    def filtered_hand_depth(self):
        return self.msgs[rt.FILTERED_HAND_DEPTH]

    @property
    def filtered_head_depth(self):
        return self.msgs[rt.FILTERED_HEAD_DEPTH]

    @property
    def filtered_hand_rgb(self):
        return self.msgs[rt.HAND_RGB]

    def detections_cb(self, msg):
        timestamp, detections_str = msg.data.split("|")
        self.detections_buffer["detections"][int(timestamp)] = detections_str

    def img_callback(self, topic, msg):
        super().img_callback(topic, msg)
        if topic == rt.MASK_RCNN_VIZ_TOPIC or topic == rt.OWLVIT_VIZ_TOPIC:
            self.detections_buffer["viz"][int(msg.header.stamp.nsecs)] = msg
        elif topic == rt.FILTERED_HAND_DEPTH:
            self.detections_buffer["filtered_depth"][int(msg.header.stamp.nsecs)] = msg

    def say(self, *args):
        text = " ".join(args)
        print("[base_env.py]: Saying:", text)
        self.tts_pub.publish(String(text))

    def reset(self, target_obj_id=None, *args, **kwargs):
        # Reset parameters
        self.num_steps = 0
        self.reset_ran = True
        self.grasp_attempted = False
        self.use_mrcnn = self.config.USE_MRCNN
        self.locked_on_object_count = 0
        self.curr_forget_steps = 0
        self.target_obj_name = target_obj_id
        self.last_target_obj = None
        self.obj_center_pixel = None
        self.place_attempted = False
        self.detection_timestamp = -1
        self.slowdown_base = -1
        self.prev_base_moved = False
        self.should_end = False

        self.owlvit_pick_up_object_name = None

        observations = self.get_observations()
        return observations

    def step(
        self,
        base_action=None,
        arm_action=None,
        grasp=False,
        place=False,
        max_joint_movement_key="MAX_JOINT_MOVEMENT",
        nav_silence_only=True,
        disable_oa=None,
    ):
        """Moves the arm and returns updated observations
        :param base_action: np.array of velocities (linear, angular)
        :param arm_action: np.array of radians denoting how each joint is to be moved
        :param grasp: whether to call the grasp_hand_depth() method
        :param place: whether to call the open_gripper() method
        :param max_joint_movement_key: max allowable displacement of arm joints
            (different for gaze and place)
        :return: observations, reward (None), done, info
        """
        assert self.reset_ran, ".reset() must be called first!"
        target_yaw = None
        if disable_oa is None:
            disable_oa = self.config.DISABLE_OBSTACLE_AVOIDANCE
        grasp = grasp or self.config.GRASP_EVERY_STEP
        print(f"raw_base_ac: {arr2str(base_action)}\traw_arm_ac: {arr2str(arm_action)}")
        if grasp:
            # Briefly pause and get latest gripper image to ensure precise grasp
            time.sleep(0.5)
            self.get_gripper_images(save_image=False)

            if self.curr_forget_steps == 0:
                print(f"GRASP CALLED: Aiming at (x, y): {self.obj_center_pixel}!")
                self.say("Grasping " + "object")

                # The following cmd is blocking
                success = self.attempt_grasp()
                if success:
                    # Just leave the object on the receptacle if desired
                    if self.config.DONT_PICK_UP:
                        print("open_gripper in don't pick up")
                        self.spot.open_gripper()
                    self.grasp_attempted = True
                    arm_positions = np.deg2rad(self.config.PLACE_ARM_JOINT_ANGLES)
                else:
                    self.say("BD grasp API failed.")
                    self.spot.open_gripper()
                    self.locked_on_object_count = 0
                    arm_positions = np.deg2rad(self.config.GAZE_ARM_JOINT_ANGLES)
                    time.sleep(2)

                # Revert joint positions after grasp
                self.spot.set_arm_joint_positions(
                    positions=arm_positions, travel_time=1.0
                )
                # Wait for arm to return to position
                time.sleep(1.0)
                if self.config.TERMINATE_ON_GRASP:
                    self.should_end = True
        elif place:
            print("PLACE ACTION CALLED: Opening the gripper!")
            if self.get_grasp_angle_to_xy() < np.deg2rad(30):
                self.turn_wrist()
                print("open gripper in place")
            self.spot.open_gripper()
            time.sleep(0.3)
            self.place_attempted = True
        if base_action is not None:
            if nav_silence_only:
                base_action = rescale_actions(base_action, silence_only=True)
            else:
                base_action = np.clip(base_action, -1, 1)
            if np.count_nonzero(base_action) > 0:
                # Command velocities using the input action
                lin_dist, ang_dist = base_action
                lin_dist *= self.max_lin_dist
                ang_dist *= self.max_ang_dist
                target_yaw = wrap_heading(self.yaw + ang_dist)
                # No horizontal velocity
                ctrl_period = 1 / self.ctrl_hz
                # Don't even bother moving if it's just for a bit of distance
                if abs(lin_dist) < 0.05 and abs(ang_dist) < np.deg2rad(3):
                    base_action = None
                    target_yaw = None
                else:
                    base_action = [lin_dist / ctrl_period, 0, ang_dist / ctrl_period]
                    self.prev_base_moved = True
            else:
                base_action = None
                self.prev_base_moved = False
        if arm_action is not None:
            arm_action = rescale_actions(arm_action)
            if np.count_nonzero(arm_action) > 0:
                arm_action *= self.config[max_joint_movement_key]
                arm_action = self.current_arm_pose + pad_action(arm_action)
                arm_action = np.clip(
                    arm_action, self.arm_lower_limits, self.arm_upper_limits
                )
            else:
                arm_action = None

        if not (grasp or place):
            if self.slowdown_base > -1 and base_action is not None:
                # self.ctrl_hz = self.slowdown_base
                base_action = np.array(base_action) * self.slowdown_base # / self.ctrl_hz
            if base_action is not None and arm_action is not None:
                self.spot.set_base_vel_and_arm_pos(
                    *base_action,
                    arm_action,
                    MAX_CMD_DURATION,
                    disable_obstacle_avoidance=disable_oa,
                )
            elif base_action is not None:
                self.spot.set_base_velocity(
                    *base_action,
                    MAX_CMD_DURATION,
                    disable_obstacle_avoidance=disable_oa,
                )
            elif arm_action is not None:
                self.spot.set_arm_joint_positions(
                    positions=arm_action, travel_time=1 / self.ctrl_hz * 0.9
                )

        if self.prev_base_moved and base_action is None:
            self.spot.stand()

        print(f"base_action: {arr2str(base_action)}\tarm_action: {arr2str(arm_action)}")

        # Spin until enough time has passed during this step
        start_time = time.time()
        if base_action is not None or arm_action is not None:
            while time.time() < start_time + 1 / self.ctrl_hz:
                if target_yaw is not None and abs(
                    wrap_heading(self.yaw - target_yaw)
                ) < np.deg2rad(3):
                    # Prevent overshooting of angular velocity
                    self.spot.set_base_velocity(base_action[0], 0, 0, MAX_CMD_DURATION)
                    target_yaw = None
        elif not (grasp or place):
            print("!!!! NO ACTIONS CALLED: moving to next step !!!!")
            self.num_steps -= 1

        self.stopwatch.record("run_actions")
        if base_action is not None:
            self.spot.set_base_velocity(0, 0, 0, 0.5)

        observations = self.get_observations()
        self.stopwatch.record("get_observations")

        self.num_steps += 1
        timeout = self.num_steps >= self.max_episode_steps
        done = timeout or self.get_success(observations) or self.should_end
        self.ctrl_hz = self.config.CTRL_HZ  # revert ctrl_hz in case it slowed down

        # Don't need reward or info
        reward = None
        info = {"num_steps": self.num_steps}

        return observations, reward, done, info

    def attempt_grasp(self):
        pre_grasp = time.time()
        ret = self.spot.grasp_hand_depth(
            self.obj_center_pixel, top_down_grasp=True, timeout=10
        )
        if self.config.USE_REMOTE_SPOT:
            ret = time.time() - pre_grasp > 3  # TODO: Make this better...
        return ret

    @staticmethod
    def get_nav_success(observations, success_distance, success_angle):
        # Is the agent at the goal?
        dist_to_goal, _ = observations["target_point_goal_gps_and_compass_sensor"]
        at_goal = dist_to_goal < success_distance
        good_heading = abs(observations["goal_heading"][0]) < success_angle
        return at_goal and good_heading

    def print_nav_stats(self, observations):
        rho, theta = observations["target_point_goal_gps_and_compass_sensor"]
        print(
            f"Dist to goal: {rho:.2f}\t"
            f"theta: {np.rad2deg(theta):.2f}\t"
            f"x: {self.x:.2f}\t"
            f"y: {self.y:.2f}\t"
            f"yaw: {np.rad2deg(self.yaw):.2f}\t"
            f"gh: {np.rad2deg(observations['goal_heading'][0]):.2f}\t"
        )

    def get_nav_observation(self, goal_xy, goal_heading):
        observations = {}

        # Get visual observations
        front_depth = self.msg_to_cv2(self.filtered_head_depth, "mono8")

        front_depth = cv2.resize(
            front_depth, (120 * 2, 212), interpolation=cv2.INTER_AREA
        )
        front_depth = np.float32(front_depth) / 255.0
        # Add dimension for channel (unsqueeze)
        front_depth = front_depth.reshape(*front_depth.shape[:2], 1)
        observations["spot_right_depth"], observations["spot_left_depth"] = np.split(
            front_depth, 2, 1
        )

        # Get rho theta observation
        curr_xy = np.array([self.x, self.y], dtype=np.float32)
        rho = np.linalg.norm(curr_xy - goal_xy)
        theta = wrap_heading(
            np.arctan2(goal_xy[1] - self.y, goal_xy[0] - self.x) - self.yaw
        )
        rho_theta = np.array([rho, theta], dtype=np.float32)

        # Get goal heading observation
        goal_heading_ = -np.array(
            [wrap_heading(goal_heading - self.yaw)], dtype=np.float32
        )
        observations["target_point_goal_gps_and_compass_sensor"] = rho_theta
        observations["goal_heading"] = goal_heading_

        return observations

    def get_arm_joints(self):
        # Get proprioception inputs
        joints = np.array(
            [
                j
                for idx, j in enumerate(self.current_arm_pose)
                if idx not in self.config.JOINT_BLACKLIST
            ],
            dtype=np.float32,
        )

        return joints

    def get_gripper_images(self, save_image=False):
        if self.grasp_attempted:
            # Return blank images if the gripper is being blocked
            blank_img = np.zeros([NEW_HEIGHT, NEW_WIDTH, 1], dtype=np.float32)
            return blank_img, blank_img.copy()
        if self.parallel_inference_mode:
            self.detection_timestamp = None
            # Use .copy() to prevent mutations during iteration
            for i in reversed(self.detections_buffer["detections"].copy()):
                if (
                    i in self.detections_buffer["detections"]
                    and i in self.detections_buffer["filtered_depth"]
                ):
                    self.detection_timestamp = i
                    break
            if self.detection_timestamp is None:
                raise RuntimeError("Could not correctly synchronize gaze observations")
            self.detections_str_synced, filtered_hand_depth = (
                self.detections_buffer["detections"][self.detection_timestamp],
                self.detections_buffer["filtered_depth"][self.detection_timestamp],
            )
            arm_depth = self.msg_to_cv2(filtered_hand_depth, "mono8")
        else:
            arm_depth = self.msg_to_cv2(self.filtered_hand_depth, "mono8")

        # Crop out black vertical bars on the left and right edges of aligned depth img
        arm_depth = arm_depth[:, LEFT_CROP:-RIGHT_CROP]
        arm_depth = cv2.resize(
            arm_depth, (NEW_WIDTH, NEW_HEIGHT), interpolation=cv2.INTER_AREA
        )
        arm_depth = arm_depth.reshape([*arm_depth.shape, 1])  # unsqueeze
        arm_depth = np.float32(arm_depth) / 255.0

        # Generate object mask channel
        obj_bbox = self.update_gripper_detections(arm_depth, save_image)

        if obj_bbox is not None:
            self.target_object_distance, arm_depth_bbox = get_obj_dist_and_bbox(
                obj_bbox, arm_depth
            )
        else:
            self.target_object_distance = -1
            arm_depth_bbox = np.zeros_like(arm_depth, dtype=np.float32)

        return arm_depth, arm_depth_bbox

    def update_gripper_detections(self, arm_depth, save_image=False):
        if self.use_mrcnn:
            det = self.get_mrcnn_det(arm_depth, save_image=save_image)
        else:
            det = self.get_owlvit_det(arm_depth, save_image=save_image)
        print("upddate gripper")
        if det is None:
            self.curr_forget_steps += 1
            self.locked_on_object_count = 0
        print("here")
        return det

    def get_mrcnn_det(self, arm_depth, save_image=False):
        marked_img = None
        if self.parallel_inference_mode:
            detections_str = str(self.detections_str_synced)
        else:
            img = self.msg_to_cv2(self.msgs[rt.HAND_RGB])
            if save_image:
                marked_img = img.copy()
            pred = generate_mrcnn_detections(
                img,
                scale=self.config.IMAGE_SCALE,
                mrcnn=self.mrcnn,
                grayscale=True,
                deblurgan=self.deblur_gan,
            )
            detections_str = pred2string(pred)

        # If we haven't seen the current target object in a while, look for new ones
        if self.curr_forget_steps >= self.forget_target_object_steps:
            self.target_obj_name = None

        if detections_str != "None":
            detected_classes = [
                object_id_to_object_name(int(i.split(",")[0]))
                for i in detections_str.split(";")
            ]
            print("[mask_rcnn]: Detected:", ", ".join(detected_classes))

            if self.target_obj_name is None:
                most_confident_class_id = None
                most_confident_score = 0.0
                good_detections = []
                for det in detections_str.split(";"):
                    class_id, score = det.split(",")[:2]
                    class_id, score = int(class_id), float(score)
                    dist = get_obj_dist_and_bbox(self.get_det_bbox(det), arm_depth)[0]
                    if score > 0.8 and dist < MAX_HAND_DEPTH:
                        good_detections.append(object_id_to_object_name(class_id))
                        if score > most_confident_score:
                            most_confident_score = score
                            most_confident_class_id = class_id
                if most_confident_score == 0.0:
                    return None
                most_confident_name = object_id_to_object_name(most_confident_class_id)
                if most_confident_name in self.last_seen_objs:
                    self.target_obj_name = most_confident_name
                    if self.target_obj_name != self.last_target_obj:
                        self.say("Now targeting " + self.target_obj_name)
                    self.last_target_obj = self.target_obj_name
                self.last_seen_objs = good_detections
            else:
                self.last_seen_objs = []
        else:
            return None

        # Check if desired object is in view of camera
        targ_obj_name = self.target_obj_name

        def correct_class(detection):
            return (
                object_id_to_object_name(int(detection.split(",")[0])) == targ_obj_name
            )

        matching_detections = [d for d in detections_str.split(";") if correct_class(d)]
        if not matching_detections:
            return None

        self.curr_forget_steps = 0

        # Get object match with the highest score
        def get_score(detection):
            return float(detection.split(",")[1])

        best_detection = sorted(matching_detections, key=get_score)[-1]
        x1, y1, x2, y2 = self.get_det_bbox(best_detection)

        # Create bbox mask from selected detection
        cx = int(np.mean([x1, x2]))
        cy = int(np.mean([y1, y2]))
        self.obj_center_pixel = (cx, cy)

        if save_image:
            if marked_img is None:
                while self.detection_timestamp not in self.detections_buffer["viz"]:
                    pass
                viz_img = self.detections_buffer["viz"][self.detection_timestamp]
                marked_img = self.cv_bridge.imgmsg_to_cv2(viz_img)
                marked_img = cv2.resize(
                    marked_img,
                    (0, 0),
                    fx=1 / self.config.IMAGE_SCALE,
                    fy=1 / self.config.IMAGE_SCALE,
                    interpolation=cv2.INTER_AREA,
                )
            marked_img = cv2.circle(marked_img, (cx, cy), 5, (0, 0, 255), -1)
            marked_img = cv2.rectangle(marked_img, (x1, y1), (x2, y2), (0, 0, 255))
            out_path = osp.join(GRASP_VIS_DIR, f"{time.time()}.png")
            cv2.imwrite(out_path, marked_img)
            print("Saved grasp image as", out_path)
            img_msg = self.cv_bridge.cv2_to_imgmsg(marked_img)
            self.mrcnn_viz_pub.publish(img_msg)

        height, width = (480, 640)
        locked_on = self.locked_on_object(x1, y1, x2, y2, height, width)
        if locked_on:
            self.locked_on_object_count += 1
            print(f"Locked on to target {self.locked_on_object_count} time(s)...")
        else:
            if self.locked_on_object_count > 0:
                print("Lost lock-on!")
            self.locked_on_object_count = 0

        return x1, y1, x2, y2

    def get_owlvit_det(self, arm_depth, save_image=False):
        #time.sleep(0.75)
        print("vvvv")
        print("save image", save_image)
        marked_img = None
        if self.parallel_inference_mode:
            bbox_xy = str(self.detections_str_synced)
        else:
            # Get the image of the hand
            if USE_IMG_RESPONSE:
                # Get Spot camera image
                image_responses = self.spot.get_image_responses(IMG_SOURCES, quality=None)
                for image_response, source in zip(image_responses, IMG_SOURCES):
                    if source is SpotCamIds.HAND_COLOR:
                        img = image_response_to_cv2(image_response, reorient=True)
            else:
                bbox_xy = self.msg_to_cv2(self.msgs[rt.HAND_RGB])

            if save_image:
                marked_img = img.copy()

            self.owlvit.update_label([[self.owlvit_pick_up_object_name]])
            bbox_xy = self.owlvit.run_inference(img)

            self.curr_forget_steps = 0

        # Reset tue cur_forget_steps to avoid grasping fail
        self.curr_forget_steps = 0
        print("bbox_xy:", bbox_xy)
        if bbox_xy is None or bbox_xy == 'None':
            return None

        if USE_IMG_RESPONSE:
            x1, y1, x2, y2 = bbox_xy
        else:
            x1, y1, x2, y2 = bbox_xy.split(',')
            x1 = int(x1)
            y1 = int(y1)
            x2 = int(x2)
            y2 = int(y2)

        # Create bbox mask from selected detection
        cx = int(np.mean([x1, x2]))
        cy = int(np.mean([y1, y2]))
        self.obj_center_pixel = (cx, cy)

        if save_image:
            if marked_img is None:
                while self.detection_timestamp not in self.detections_buffer['viz']:
                    pass
                viz_img = self.detections_buffer['viz'][self.detection_timestamp]
                marked_img = self.cv_bridge.imgmsg_to_cv2(viz_img)
                marked_img = cv2.resize(
                    marked_img,
                    (0, 0),
                    fx=1 / self.config.IMAGE_SCALE,
                    fy=1 / self.config.IMAGE_SCALE,
                    interpolation=cv2.INTER_AREA,
                )
            marked_img = cv2.circle(marked_img, (cx, cy), 5, (0, 0, 255), -1)
            marked_img = cv2.rectangle(marked_img, (x1, y1), (x2, y2), (0, 0, 255))
            out_path = osp.join(GRASP_VIS_DIR, f'{time.time()}.png')
            cv2.imwrite(out_path, marked_img)

        height, width = (480, 640)
        locked_on = self.locked_on_object(x1, y1, x2, y2, height, width)
        if locked_on:
            self.locked_on_object_count += 1
            print(f'Locked on to target {self.locked_on_object_count} time(s)...')
        else:
            if self.locked_on_object_count > 0:
                print('Lost lock-on!')
            self.locked_on_object_count = 0
        print("aaa")

        print("bbb")
        return x1, y1, x2, y2

    def get_det_bbox(self, det):
        img_scale_factor = self.config.IMAGE_SCALE
        x1, y1, x2, y2 = [int(float(i) / img_scale_factor) for i in det.split(",")[-4:]]
        return x1, y1, x2, y2

    @staticmethod
    def locked_on_object(x1, y1, x2, y2, height, width, radius=0.15):
        cy, cx = height // 2, width // 2
        # Locked on if the center of the image is in the bbox
        if x1 < cx < x2 and y1 < cy < y2:
            return True

        pixel_radius = min(height, width) * radius
        # Get pixel distance between bbox rectangle and the center of the image
        # Stack Overflow question ID #5254838
        dx = np.max([x1 - cx, 0, cx - x2])
        dy = np.max([y1 - cy, 0, cy - y2])
        bbox_dist = np.sqrt(dx ** 2 + dy ** 2)
        locked_on = bbox_dist < pixel_radius

        return locked_on

    def should_grasp(self):
        print("grasp here")
        grasp = False
        if self.locked_on_object_count >= self.config.OBJECT_LOCK_ON_NEEDED:
            if self.target_object_distance < 1.5:
                if self.config.ASSERT_CENTERING:
                    x, y = self.obj_center_pixel
                    if abs(x / 640 - 0.5) < 0.25 or abs(y / 480 - 0.5) < 0.25:
                        grasp = True
                    else:
                        print("Too off center to grasp!:", x / 640, y / 480)
            else:
                print(f"Too far to grasp ({self.target_object_distance})!")

        return grasp

    def get_observations(self):
        raise NotImplementedError

    def get_success(self, observations):
        raise NotImplementedError

    @staticmethod
    def spot2habitat_transform(position, rotation):
        x, y, z = position
        qx, qy, qz, qw = rotation

        quat = quaternion.quaternion(qw, qx, qy, qz)
        rotation_matrix = mn.Quaternion(quat.imag, quat.real).to_matrix()
        rotation_matrix_fixed = (
            rotation_matrix
            @ mn.Matrix4.rotation(
                mn.Rad(-np.pi / 2.0), mn.Vector3(1.0, 0.0, 0.0)
            ).rotation()
        )
        translation = mn.Vector3(x, z, -y)

        quat_rotated = mn.Quaternion.from_matrix(rotation_matrix_fixed)
        quat_rotated.vector = mn.Vector3(
            quat_rotated.vector[0], quat_rotated.vector[2], -quat_rotated.vector[1]
        )
        rotation_matrix_fixed = quat_rotated.to_matrix()
        sim_transform = mn.Matrix4.from_(rotation_matrix_fixed, translation)

        return sim_transform

    @staticmethod
    def spot2habitat_translation(spot_translation):
        return mn.Vector3(np.array(spot_translation)[np.array([0, 2, 1])])

    @property
    def curr_transform(self):
        # Assume body is at default height of 0.5 m
        # This is local_T_global.
        return mn.Matrix4.from_(
            mn.Matrix4.rotation_z(mn.Rad(self.yaw)).rotation(),
            mn.Vector3(self.x, self.y, 0.5),
        )

    def get_place_sensor(self):
        # The place goal should be provided relative to the local robot frame given that
        # the robot is at the place receptacle
        gripper_T_base = self.get_in_gripper_tf()
        base_T_gripper = gripper_T_base.inverted()
        base_frame_place_target = self.get_base_frame_place_target()
        hab_place_target = self.spot2habitat_translation(base_frame_place_target)
        gripper_pos = base_T_gripper.transform_point(hab_place_target)

        return gripper_pos

    def get_base_frame_place_target(self):
        if self.place_target_is_local:
            base_frame_place_target = self.place_target
        else:
            base_frame_place_target = self.get_target_in_base_frame(self.place_target)
        return base_frame_place_target

    def get_place_distance(self):
        gripper_T_base = self.get_in_gripper_tf()
        base_frame_gripper_pos = np.array(gripper_T_base.translation)
        base_frame_place_target = self.get_base_frame_place_target()
        hab_place_target = self.spot2habitat_translation(base_frame_place_target)
        hab_place_target = np.array(hab_place_target)
        place_dist = np.linalg.norm(hab_place_target - base_frame_gripper_pos)
        xy_dist = np.linalg.norm(
            hab_place_target[[0, 2]] - base_frame_gripper_pos[[0, 2]]
        )
        z_dist = abs(hab_place_target[1] - base_frame_gripper_pos[1])
        return place_dist, xy_dist, z_dist

    def get_in_gripper_tf(self):
        wrist_T_base = self.spot2habitat_transform(
            self.link_wr1_position, self.link_wr1_rotation
        )
        gripper_T_base = wrist_T_base @ mn.Matrix4.translation(self.ee_gripper_offset)

        return gripper_T_base

    def get_target_in_base_frame(self, place_target):
        global_T_local = self.curr_transform.inverted()
        local_place_target = np.array(global_T_local.transform_point(place_target))
        local_place_target[1] *= -1  # Still not sure why this is necessary

        return local_place_target

    def get_grasp_object_angle(self, obj_translation):
        """Calculates angle between gripper line-of-sight and given global position"""
        camera_T_matrix = self.get_gripper_transform()

        # Get object location in camera frame
        camera_obj_trans = (
            camera_T_matrix.inverted().transform_point(obj_translation).normalized()
        )

        # Get angle between (normalized) location and unit vector
        object_angle = angle_between(camera_obj_trans, mn.Vector3(0, 0, -1))

        return object_angle

    def get_grasp_angle_to_xy(self):
        gripper_tf = self.get_in_gripper_tf()
        gripper_cam_position = gripper_tf.translation
        below_gripper = gripper_cam_position + mn.Vector3(0.0, -1.0, 0.0)

        # Get below gripper pos in gripper frame
        gripper_obj_trans = (
            gripper_tf.inverted().transform_point(below_gripper).normalized()
        )

        # Get angle between (normalized) location and unit vector
        object_angle = angle_between(gripper_obj_trans, mn.Vector3(0, 0, -1))

        return object_angle

    def turn_wrist(self):
        arm_positions = np.array(self.current_arm_pose)
        arm_positions[-1] = np.deg2rad(90)
        self.spot.set_arm_joint_positions(positions=arm_positions, travel_time=0.3)
        time.sleep(0.6)

    def power_robot(self):
        self.spot.power_on()
        # self.say("Standing up")
        try:
            self.spot.undock()
        except:
            print("Undocking failed: just standing up instead...")
            self.spot.blocking_stand()


def get_obj_dist_and_bbox(obj_bbox, arm_depth):
    x1, y1, x2, y2 = obj_bbox
    x1 = max(int(float(x1 - LEFT_CROP) * WIDTH_SCALE), 0)
    x2 = max(int(float(x2 - LEFT_CROP) * WIDTH_SCALE), 0)
    y1 = int(float(y1) * HEIGHT_SCALE)
    y2 = int(float(y2) * HEIGHT_SCALE)
    arm_depth_bbox = np.zeros_like(arm_depth, dtype=np.float32)
    arm_depth_bbox[y1:y2, x1:x2] = 1.0

    # Estimate distance from the gripper to the object
    depth_box = arm_depth[y1:y2, x1:x2]

    return np.median(depth_box) * MAX_HAND_DEPTH, arm_depth_bbox


def angle_between(v1, v2):
    # stack overflow question ID: 2827393
    cosine = np.clip(np.dot(v1, v2), -1.0, 1.0)
    object_angle = np.arccos(cosine)

    return object_angle