from __future__ import annotations
import subprocess
import psutil
from typing import List, Optional
import os
from urllib.parse import urlparse
import tempfile
import time

import pycdlib
import requests
from namesgenerator import get_random_name

from .base import DesktopVM, DesktopProvider
from .img import JAMMY
from agentdesk.server.models import V1ProviderData
from agentdesk.util import (
    check_command_availability,
    find_ssh_public_key,
)
from agentdesk.proxy import SSHPortForwarding

META_PYTHON_IMAGE = "python:3.9-slim"
META_CONTAINER_NAME = "http_server"


class QemuProvider(DesktopProvider):
    """A VM provider using local QEMU virtual machines."""

    def __init__(self, log_vm: bool = False) -> None:
        self.log_vm = log_vm

    def create(
        self,
        name: Optional[str] = None,
        image: Optional[str] = None,
        memory: int = 4,
        cpu: int = 2,
        disk: str = "30gb",
        reserve_ip: bool = False,
        ssh_key: Optional[str] = None,
    ) -> DesktopVM:
        """Create a local QEMU VM locally"""

        if not check_command_availability("qemu-system-x86_64"):
            raise EnvironmentError(
                "qemu-system-x86_64 is not installed. Please install QEMU."
            )

        if not name:
            name = get_random_name()

        # Directory to store VM images
        vm_dir = os.path.expanduser(f"~/.agentsea/vms")
        os.makedirs(vm_dir, exist_ok=True)

        if not image:
            image = JAMMY.qcow2
            image_name = JAMMY.name
        elif image.startswith("https://"):
            parsed_url = urlparse(image)
            image_name = parsed_url.hostname + parsed_url.path.replace("/", "_")
        else:
            image = os.path.expanduser(image)
            if not os.path.exists(image):
                raise FileNotFoundError(
                    f"The specified image path '{image}' does not exist."
                )
            image_name = os.path.basename(image)

        image_path = os.path.join(vm_dir, image_name)
        print("image path: ", image_path)

        # Download image only if it does not exist
        if not os.path.exists(image_path) and image.startswith("https://"):
            print(f"downloading image '{image}'...")
            response = requests.get(image, stream=True)
            print("response: ", response)
            with open(image_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

        # Find or generate an SSH key if not provided
        ssh_key = ssh_key or find_ssh_public_key()
        if not ssh_key:
            raise ValueError("SSH key not provided or found")

        print("generating cloud config with ssh key: ", ssh_key)
        # Generate user-data
        user_data = f"""#cloud-config
users:
  - name: agentsea
    ssh_authorized_keys:
      - { ssh_key }
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: sudo
    shell: /bin/bash
"""
        meta_data = f"""instance-id: {name}
local-hostname: {name}
"""
        sockify_port: int = 6080
        agentd_port: int = 8000
        ssh_port = 2222

        self._create_iso("cidata.iso", user_data, meta_data)

        command = (
            f"qemu-system-x86_64 -nographic -hda {image_path} -m {memory}G "
            f"-smp {cpu} -netdev user,id=vmnet,hostfwd=tcp::5900-:5900,hostfwd=tcp::{sockify_port}-:6080,hostfwd=tcp::{agentd_port}-:8000,hostfwd=tcp::{ssh_port}-:22 "
            # f"-smp {cpu} -netdev user,id=vmnet,hostfwd=tcp::{ssh_port}-:22 "
            "-device e1000,netdev=vmnet "
            f"-cdrom cidata.iso"
        )

        # Start the QEMU process
        if self.log_vm:
            process = subprocess.Popen(command, shell=True)
        else:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        ready = False
        while not ready:
            print("waiting for desktop to be ready...")
            time.sleep(3)
            try:
                with SSHPortForwarding() as connection:
                    response = requests.get(
                        f"http://localhost:{connection.local_port}/health"
                    )
                    if response.status_code == 200:
                        print("\n---desktop ready!")
                        ready = True
            except:
                pass

        # Create and return a Desktop object
        desktop = DesktopVM(
            name=name,
            addr="localhost",
            cpu=cpu,
            memory=memory,
            disk=disk,
            pid=process.pid,
            image=image,
            provider=self.to_data(),
        )
        return desktop

    def _create_iso(self, output_iso: str, user_data: str, meta_data: str) -> None:
        iso = pycdlib.PyCdlib()
        iso.new(joliet=3, rock_ridge="1.09", vol_ident="cidata")

        # Use the tempfile module to create temporary files for user-data and meta-data
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False
        ) as user_data_file, tempfile.NamedTemporaryFile(
            mode="w", delete=False
        ) as meta_data_file:
            user_data_file.write(user_data)
            meta_data_file.write(meta_data)

            user_data_path = user_data_file.name
            meta_data_path = meta_data_file.name

        # Add user-data and meta-data files
        iso.add_file(
            user_data_path,
            "/USERDATA.;1",
            joliet_path="/USERDATA.;1",
            rr_name="user-data",
        )
        iso.add_file(
            meta_data_path,
            "/METADATA.;1",
            joliet_path="/METADATA.;1",
            rr_name="meta-data",
        )

        # Write to an ISO file
        iso.write(output_iso)
        iso.close()

        # Clean up the temporary files
        os.remove(user_data_path)
        os.remove(meta_data_path)

    def delete(self, name: str) -> None:
        """Delete a local QEMU VM."""
        desktop = DesktopVM.load(name)
        if psutil.pid_exists(desktop.pid):
            process = psutil.Process(desktop.pid)
            process.terminate()
            process.wait()
        DesktopVM.delete(desktop.id)

    def start(self, name: str) -> None:
        """Start a local QEMU VM."""
        # Starting a local VM might be equivalent to creating it, as QEMU processes don't persist.
        raise NotImplementedError(
            "Start method is not available for QEMU VMs. Use create() instead."
        )

    def stop(self, name: str) -> None:
        """Stop a local QEMU VM."""
        self.delete(name)

    def list(self) -> List[DesktopVM]:
        """List local QEMU VMs."""
        desktops = DesktopVM.list()
        return [
            desktop
            for desktop in desktops
            if isinstance(desktop.provider, V1ProviderData)
            and desktop.provider.type == "qemu"
        ]

    def get(self, name: str) -> Optional[DesktopVM]:
        """Get a local QEMU VM."""
        try:
            desktop = DesktopVM.load(name)
            if (
                isinstance(desktop.provider, V1ProviderData)
                and desktop.provider.type == "qemu"
            ):
                return desktop
            return None
        except ValueError:
            return None

    def to_data(self) -> V1ProviderData:
        """Convert to a ProviderData object."""
        return V1ProviderData(type="qemu", args={"log_vm": self.log_vm})

    @classmethod
    def from_data(cls, data: V1ProviderData) -> QemuProvider:
        """Create a provider from ProviderData."""
        return cls(**data.args)
