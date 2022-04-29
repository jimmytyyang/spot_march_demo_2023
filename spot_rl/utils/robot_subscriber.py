from collections import defaultdict
from functools import partial

import numpy as np
import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image
from spot_wrapper.spot import Spot
from std_msgs.msg import Float32MultiArray

from spot_rl.utils.utils import ros_topics as rt

IMG_TOPICS = [
    rt.MASK_RCNN_VIZ_TOPIC,
    rt.HEAD_DEPTH,
    rt.HAND_DEPTH,
    rt.HAND_RGB,
    rt.FILTERED_HEAD_DEPTH,
    rt.FILTERED_HAND_DEPTH,
]
NO_RAW_IMG_TOPICS = [
    rt.MASK_RCNN_VIZ_TOPIC,
    rt.HAND_RGB,
    rt.FILTERED_HEAD_DEPTH,
    rt.FILTERED_HAND_DEPTH,
]


class SpotRobotSubscriberMixin:
    node_name = "SpotRobotSubscriber"
    no_raw = False
    proprioception = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rospy.init_node(self.node_name, disable_signals=True)
        self.cv_bridge = CvBridge()

        # Maps a topic name to the latest msg from it
        self.msgs = defaultdict(lambda x: None)

        subscriptions = NO_RAW_IMG_TOPICS if self.no_raw else IMG_TOPICS
        for img_topic in subscriptions:
            rospy.Subscriber(
                img_topic,
                Image,
                partial(self.img_callback, img_topic),
                queue_size=1,
                buff_size=2 ** 30,
            )

        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0
        self.current_arm_pose = None
        self.link_wr1_position, self.link_wr1_rotation = None, None
        if self.proprioception:
            rospy.Subscriber(
                rt.ROBOT_STATE,
                Float32MultiArray,
                self.robot_state_callback,
                queue_size=1,
            )
            self.spot = Spot(self.node_name)
        else:
            self.spot = None

        rospy.loginfo(f"[{self.node_name}]: Robot subscribing has started.")

    def img_callback(self, topic, msg):
        self.msgs[topic] = msg

    def robot_state_callback(self, msg):
        x, y, yaw = msg.data[:3]
        self.x, self.y, self.yaw = self.spot.xy_yaw_global_to_home(x, y, yaw)
        self.current_arm_pose = msg.data[3:-7]
        self.link_wr1_position, self.link_wr1_rotation = (
            msg.data[-7:][:3],
            msg.data[-7:][3:],
        )

    def msg_to_cv2(self, *args, **kwargs) -> np.array:
        return self.cv_bridge.imgmsg_to_cv2(*args, **kwargs)
