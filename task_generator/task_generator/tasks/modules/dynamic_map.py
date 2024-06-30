import json
import os
from typing import Dict
import map_distance_server.srv as map_distance_server_srvs
import std_msgs.msg as std_msgs
import nav_msgs.msg as nav_msgs
from task_generator.constants import Constants

from task_generator.manager.entity_manager.utils import ObstacleLayer
from task_generator.manager.utils import WorldMap

import rospy
from task_generator.tasks.modules import TM_Module
from task_generator.tasks.task_factory import TaskFactory
from task_generator.utils import rosparam_get

from map_generator.constants import MAP_GENERATOR_NS

import dynamic_reconfigure.client

# DYNAMIC MAP INTERFACE

DynamicMapConfiguration = Dict[str, Dict]


@TaskFactory.register_module(Constants.TaskMode.TM_Module.DYNAMIC_MAP)
class Mod_DynamicMap(TM_Module):
    """
    This class represents a module for generating and managing dynamic maps in the task generator.
    It provides functionality for requesting new maps, resetting tasks, and updating the map based on distance information.
    """

    __map_request_pub: rospy.Publisher
    __task_reset_pub: rospy.Publisher
    __get_dist_map_service: rospy.ServiceProxy

    PARAM_MAP_FILE = "map_file"
    PARAM_EPISODES = "/dynamic_map/curr_eps"
    PARAM_GENERATOR = "generator"
    PARAM_GENERATOR_CONFIGS = "/generator_configs"

    PARAM_ALGORITHM = MAP_GENERATOR_NS("algorithm")
    PARAM_ALGORITHM_CONFIG = MAP_GENERATOR_NS("algorithm_config")

    TOPIC_REQUEST_MAP = "/request_new_map"
    TOPIC_RESET = "/dynamic_map/task_reset"
    TOPIC_MAP = "/map"
    TOPIC_SIGNAL_MAP = "/signal_new_distance_map"

    SERVICE_DISTANCE_MAP = "/distance_map"

    def before_reset(self, **kwargs):
        """
        This method is called before resetting the task.
        It increments the number of episodes and requests a new map if the episode count exceeds the threshold.
        """

        self._target_eps_num = (
            rosparam_get(int, MAP_GENERATOR_NS("episode_per_map"), 1) * self._num_envs
        )
        self._generation_timeout = rosparam_get(
            float, MAP_GENERATOR_NS("generation_timeout"), 60
        )

        self._episodes += 1

        if self._episodes >= self._target_eps_num:
            self.request_new_map()
            self._update_map()

    def __init__(self, **kwargs):
        """
        Initializes the Mod_DynamicMap module.
        """
        TM_Module.__init__(self, **kwargs)

        rospy.Subscriber(self.TOPIC_RESET, std_msgs.String, self._cb_task_reset)

        # requests new map from map generator
        self.__map_request_pub = rospy.Publisher(
            self.TOPIC_REQUEST_MAP, std_msgs.String, queue_size=1
        )
        # task reset for all taskmanagers when one resets
        self.__task_reset_pub = rospy.Publisher(
            self.TOPIC_RESET, std_msgs.String, queue_size=1
        )

        self.__get_dist_map_service = rospy.ServiceProxy(
            self.SERVICE_DISTANCE_MAP, map_distance_server_srvs.GetDistanceMap
        )

        self._num_envs: int = (
            rosparam_get(int, "num_envs", 1)
            if "eval_sim" not in self._TASK.robot_managers[0].namespace
            else 1
        )

        dynamic_reconfigure.client.Client(
            name=self.NODE_CONFIGURATION,
            config_callback=self.reconfigure
        )

    def reconfigure(self, config):

        rospy.set_param(MAP_GENERATOR_NS("episode_per_map"), config["DYNAMICMAP_episodes"])
        rospy.set_param(MAP_GENERATOR_NS("generation_timeout"), config["DYNAMICMAP_timeout"])

        algorithm = str(config["DYNAMICMAP_algorithm"])
        rospy.set_param(self.PARAM_ALGORITHM, algorithm)
        
        config = json.loads(config["DYNAMICMAP_config"])
        assert isinstance(config, dict)

        for k,v in config.items():
            rospy.set_param(self.PARAM_ALGORITHM_CONFIG(k), v)

    def _set_config(self, config: DynamicMapConfiguration):
        """
        Sets the configuration for the map generator based on the provided DynamicMapConfiguration object.
        """
        generator = rosparam_get(str, MAP_GENERATOR_NS(self.PARAM_GENERATOR))

        log = f"Setting [Map Generator: {generator}] parameters"

        config_generator = config.get(generator, dict())

        for key, value in config_generator.items():
            log += f"\t{key}={value}"
            rospy.set_param(
                os.path.join(self.PARAM_GENERATOR_CONFIGS, generator, key), value
            )

        rospy.loginfo(log)

    def _cb_task_reset(self, *args, **kwargs):
        """
        Callback function for task reset.
        Updates the map manager and triggers map update.
        """
        # task reset for all taskmanagers when one resets
        # update map manager

        self._update_map()

    def _update_map(self):
        """
        Updates the map based on the distance information received from the service.
        """
        dist_map: map_distance_server_srvs.GetDistanceMapResponse = (
            self.__get_dist_map_service()
        )

        if isinstance(dist_map, map_distance_server_srvs.GetDistanceMapResponse):
            self._TASK.world_manager.update_world(
                world_map=WorldMap.from_distmap(distmap=dist_map)
            )
            self._TASK.obstacle_manager.reset(purge=ObstacleLayer.WORLD)
            self._TASK.obstacle_manager.spawn_world_obstacles(
                self._TASK.world_manager.world
            )

    def request_new_map(self):
        """
        Requests a new map from the map generator.
        """
        # set current eps immediately to 0 so that only one task
        # requests a new map
        self._episodes = 0

        self.__map_request_pub.publish("")

        try:
            rospy.wait_for_message(
                self.TOPIC_SIGNAL_MAP, std_msgs.String, timeout=self._generation_timeout
            )
            rospy.wait_for_message(
                self.TOPIC_MAP, nav_msgs.OccupancyGrid, timeout=self._generation_timeout
            )
        except rospy.ROSException:
            rospy.logwarn(
                "[Map Generator] Timeout while waiting for new map. Continue with current map."
            )
        else:
            rospy.loginfo("===================")
            rospy.loginfo("+++ Got new map +++")
            rospy.loginfo("===================")

        self.__task_reset_pub.publish("")

    @property
    def _episodes(self) -> float:
        """
        Property representing the current number of episodes.
        """
        try:
            return rosparam_get(float, self.PARAM_EPISODES, float("inf"))
        except Exception as e:
            rospy.logwarn(e)
            return 0

    @_episodes.setter
    def _episodes(self, value: float):
        """
        Setter for the _episodes property.
        """
        try:
            rospy.set_param(self.PARAM_EPISODES, value)
        except Exception as e:
            rospy.logwarn(e)
