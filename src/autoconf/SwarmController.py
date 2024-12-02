#!/usr/bin/env python3

from contextlib import suppress
from time import sleep
from traceback import format_exc
from threading import Thread, Lock
from typing import Any, Dict, List
from docker import DockerClient
from base64 import b64decode

from docker.models.services import Service
from Controller import Controller


class SwarmController(Controller):
    def __init__(self, docker_host):
        super().__init__("swarm")
        self.__client = DockerClient(base_url=docker_host)
        self.__internal_lock = Lock()
        self.__swarm_instances = []
        self.__swarm_services = []
        self.__swarm_configs = []
        self._logger.warning("Swarm integration is deprecated and will be removed in a future release")

    def _get_controller_instances(self) -> List[Service]:
        self.__swarm_instances = []
        services = self.__client.services.list(filters={"label": "bunkerweb.INSTANCE"})
        if not self._namespaces:
            return services
        return [
            service
            for service in services
            if any(service.attrs["Spec"]["Labels"].get("bunkerweb.NAMESPACE", "") == namespace for namespace in self._namespaces)
        ]

    def _get_controller_services(self) -> List[Service]:
        self.__swarm_services = []
        services = self.__client.services.list(filters={"label": "bunkerweb.SERVER_NAME"})
        if not self._namespaces:
            return services
        return [
            service
            for service in services
            if any(service.attrs["Spec"]["Labels"].get("bunkerweb.NAMESPACE", "") == namespace for namespace in self._namespaces)
        ]

    def _to_instances(self, controller_instance) -> List[dict]:
        self.__swarm_instances.append(controller_instance.id)
        instances = []
        instance_env = {}
        for env in controller_instance.attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Env"]:
            variable, value = env.split("=", 1)
            instance_env[variable] = value

        for task in controller_instance.tasks():
            if task["DesiredState"] != "running":
                continue
            instances.append(
                {
                    "name": task["ID"],
                    "hostname": f"{controller_instance.name}.{task['NodeID']}.{task['ID']}",
                    "type": "container",
                    "health": task["Status"]["State"] == "running",
                    "env": instance_env,
                }
            )
        return instances

    def _to_services(self, controller_service) -> List[dict]:
        self.__swarm_services.append(controller_service.id)
        service = {}
        for variable, value in controller_service.attrs["Spec"]["Labels"].items():
            if not variable.startswith("bunkerweb."):
                continue
            service[variable.replace("bunkerweb.", "", 1)] = value
        return [service]

    def get_configs(self) -> Dict[str, Dict[str, Any]]:
        self.__swarm_configs = []
        configs = {}
        for config_type in self._supported_config_types:
            configs[config_type] = {}
        for config in self.__client.configs.list(filters={"label": "bunkerweb.CONFIG_TYPE"}):
            if not config.name or not config.attrs or not config.attrs.get("Spec", {}).get("Labels", {}) or not config.attrs.get("Spec", {}).get("Data", {}):
                continue

            config_type = config.attrs["Spec"]["Labels"]["bunkerweb.CONFIG_TYPE"]
            config_name = config.name
            if config_type not in self._supported_config_types:
                self._logger.warning(
                    f"Ignoring unsupported CONFIG_TYPE {config_type} for Config {config_name}",
                )
                continue
            config_site = ""
            if "bunkerweb.CONFIG_SITE" in config.attrs["Spec"]["Labels"]:
                if not self._is_service_present(config.attrs["Spec"]["Labels"]["bunkerweb.CONFIG_SITE"]):
                    self._logger.warning(
                        f"Ignoring config {config_name} because {config.attrs['Spec']['Labels']['bunkerweb.CONFIG_SITE']} doesn't exist",
                    )
                    continue
                config_site = f"{config.attrs['Spec']['Labels']['bunkerweb.CONFIG_SITE']}/"
            configs[config_type][f"{config_site}{config_name}"] = b64decode(config.attrs["Spec"]["Data"])
            self.__swarm_configs.append(config.id)
        return configs

    def apply_config(self) -> bool:
        return self.apply(
            self._instances,
            self._services,
            configs=self._configs,
            first=not self._loaded,
        )

    def __process_event(self, event):
        if "Actor" not in event or "ID" not in event["Actor"] or "Type" not in event:
            return False
        if event["Type"] not in ("service", "config"):
            return False
        if event["Type"] == "service":
            if event["Actor"]["ID"] in self.__swarm_instances or event["Actor"]["ID"] in self.__swarm_services:
                return True
            try:
                labels = self.__client.services.get(event["Actor"]["ID"]).attrs["Spec"]["Labels"]
                return ("bunkerweb.INSTANCE" in labels or "bunkerweb.SERVER_NAME" in labels) and (
                    not self._namespaces or any(labels.get("bunkerweb.NAMESPACE", "") == namespace for namespace in self._namespaces)
                )
            except:
                return False
        if event["Type"] == "config":
            if event["Actor"]["ID"] in self.__swarm_configs:
                return True
            try:
                labels = self.__client.services.get(event["Actor"]["ID"]).attrs["Spec"]["Labels"]
                return "bunkerweb.CONFIG_TYPE" in labels and (
                    not self._namespaces or any(labels.get("bunkerweb.NAMESPACE", "") == namespace for namespace in self._namespaces)
                )
            except:
                return False
        return False

    def __event(self, event_type):
        while True:
            locked = False
            error = False
            applied = False
            try:
                for event in self.__client.events(decode=True, filters={"type": event_type}):
                    applied = False
                    self.__internal_lock.acquire()
                    locked = True
                    if not self.__process_event(event):
                        self.__internal_lock.release()
                        locked = False
                        continue

                    try:
                        to_apply = False
                        while not applied:
                            waiting = self.have_to_wait()
                            self._update_settings()
                            self._instances = self.get_instances()
                            self._services = self.get_services()
                            self._configs = self.get_configs()

                            if not to_apply and not self.update_needed(self._instances, self._services, configs=self._configs):
                                if locked:
                                    self.__internal_lock.release()
                                    locked = False
                                applied = True
                                continue

                            to_apply = True
                            if waiting:
                                sleep(1)
                                continue

                            self._logger.info(f"Caught Swarm event ({event_type}), deploying new configuration ...")
                            if not self.apply_config():
                                self._logger.error("Error while deploying new configuration")
                            else:
                                self._logger.info(
                                    "Successfully deployed new configuration 🚀",
                                )
                                self._set_autoconf_load_db()
                            applied = True
                    except BaseException:
                        self._logger.error(f"Exception while processing Swarm event ({event_type}) :\n{format_exc()}")

                    if locked:
                        self.__internal_lock.release()
                        locked = False
            except:
                self._logger.error(f"Exception while reading Swarm event ({event_type}) :\n{format_exc()}")
                error = True
            finally:
                if locked:
                    with suppress(BaseException):
                        self.__internal_lock.release()
                    locked = False
                if error is True:
                    self._logger.warning("Got exception, retrying in 10 seconds ...")
                    sleep(10)

    def process_events(self):
        self._set_autoconf_load_db()
        event_types = ("service", "config")
        threads = [Thread(target=self.__event, args=(event_type,)) for event_type in event_types]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
