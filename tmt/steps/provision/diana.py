# coding: utf-8

import dataclasses
import os
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union
from copy import deepcopy

import subprocess
import jinja2

import tmt
import tmt.steps
import tmt.steps.provision
import tmt.utils
from tmt.utils import (
    WORKDIR_ROOT,
    Command,
    Path,
    ProvisionError,
    ShellScript,
    field,
    retry_session,
    )

if TYPE_CHECKING:
    import tmt.base

DEFAULT_CONNECT_TIMEOUT = 60   # seconds
DEFAULT_USER = 'root'

KICKSTART_TEMPLATE = jinja2.Template(
"""
# partitioning
clearpart --all --initlabel
autopart

# accounts
rootpw rootoor
sshkey --username '{{ guest.user }}' '{{ guest.ssh_pubkey }}'

reboot

# packages
%packages
%end

# scripts
%post
source /etc/os-release
%end

# user provided data
{{ guest.kickstart['script'] }}
{{ guest.kickstart['pre-install'] }}
{{ guest.kickstart['post-install'] }}
"""
)

@dataclasses.dataclass
class DianaGuestData(tmt.steps.provision.GuestSshData):
    connection_uri: Optional[str] = field(
        default='qemu:///system',
        option=('-c', '--connection'),
        metavar='CONNECTION',
        help='Libvirt connection URI.')
    location: str = field(
        default=None,
        option=('-l', '--location'),
        metavar='LOCATION',
        help='URL to installation tree compatible with virt-install --location')
    user: Optional[str] = field(
        default=DEFAULT_USER,
        option=('-u', '--user'),
        metavar='USERNAME',
        help='Username to use for all guest operations.')
    instance_name: Optional[str] = None
    hardware: Optional[Any] = None
    kickstart: Dict[str, str] = field(
        default_factory=dict,
        option='--kickstart',
        metavar='KEY=VALUE',
        help='Optional Beaker kickstart to use when provisioning the guest.',
        multiple=True,
    )

@dataclasses.dataclass
class ProvisionDianaData(DianaGuestData, tmt.steps.provision.ProvisionStepData):
    pass

class GuestDiana(tmt.GuestSsh):
    """
    Libvirt VM Instance

    The following keys are expected in the 'data' dictionary::

        connection_uri: Libvirt hypervisor URI (e.g. qemu+ssh://root@example.com/system)
        location: URL to installable tree compatible by virt-install (e.g. https://download.fedoraproject.org/pub/fedora/linux/releases/38/Everything/x86_64/os/)
    """

    _data_class = DianaGuestData

    connection_uri: str
    location: str
    instance_name: Optional[str]

    @property
    def is_ready(self) -> bool:
        if self.guest_state != 'running':
            return False
        return super().is_ready

    @property
    def guest_state(self) -> str:
        return self._virsh(
            'domstate', self.instance_name,
            capture_output=True, encoding='utf-8'
        ).stdout.splitlines()[0]

    def get_guest_ip(self) -> str:
        for i in range(10):
            try:
                return self._virsh(
                    'domifaddr', self.instance_name,
                    capture_output=True, encoding='utf-8'
                ).stdout.splitlines()[-2].split()[-1].split('/')[0]
            except subprocess.CalledProcessError as e:
                time.sleep(1)
        raise e

    @property
    def _kickstart(self) -> str:
        return KICKSTART_TEMPLATE.render(guest=self)
    
    def wake(self) -> None:
        return super().wake()

    def _virsh(self, *args, **kwargs) -> subprocess.CompletedProcess:
        kwargs.setdefault('check', True)
        return subprocess.run(
            [
                'virsh',
                '-c', self.connection_uri,
            ] + list(args),
            **kwargs,
        )

    def _install(self) -> None:
        try:
            self.guest_state
            return
        except subprocess.CalledProcessError:
            pass
        hardware = deepcopy(self.hardware)
        self.verbose('progress', 'preparing for installation...', 'cyan')
        with open(self.workdir / 'ks.cfg', 'w') as ksfile:
            ksfile.write(self._kickstart)
            ksfile.flush()
            cmd = [
                    'virt-install',
                    '--connect', self.connection_uri,
                    '--name', self.instance_name,
                    '--memory', '4096',
                    '--vcpus', '4',
                    '--graphics', 'none',
                    '--extra-args', 'console=ttyS0 inst.ks=file://'+os.path.basename(ksfile.name),
                    '--initrd-inject', ksfile.name,
                    '--noreboot',
                    '--location', self.location,
                ]
            # Make sure that all hardware requirements were respected
            assert not hardware
            self.info('progress', 'installing...', 'cyan')
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
    
    def _generate_ssh_key(self) -> None:
        if self.key:
            return
        self.key = [ self.workdir / 'ssh_key' ]
        subprocess.run(
            ['ssh-keygen', '-f', self.key[0], '-N', ''],
            check=True,
        )

    @property
    def ssh_pubkey(self) -> str:
        self._generate_ssh_key()
        return subprocess.run(
            ['ssh-keygen', '-y', '-f', self.key[0]],
            capture_output=True, encoding='utf-8'
        ).stdout.rstrip('\n')

    def start(self) -> None:
        self.instance_name = self._tmt_name()
        self.verbose('instance_name', self.instance_name, 'yellow')
        if self.opt('dry'):
            return
        # Install the virtual machine
        try:
            self._install()
            # the VM is powered off after the installation
        except (subprocess.CalledProcessError) as error:
            raise ProvisionError(
                f'Failed to install OS on a libvirt guest ({error}).')
        if self.guest_state != "running": # TODO: this should not be needed
            self._virsh('start', self.instance_name)
        self.guest = self.get_guest_ip()
        self.port = 22
        self.verbose('ip', self.guest, 'green')
        self.verbose('port', str(self.port), 'green')

        # FIXME copied from Testcloud plugin
        # Wait until it's possible to connect to guest via SSH
        time_coeff = 1 # don't be smart!
        if not self.reconnect(
                timeout=DEFAULT_CONNECT_TIMEOUT *
                time_coeff,
                tick=1):
            raise ProvisionError(
                f"Failed to connect in {DEFAULT_CONNECT_TIMEOUT * time_coeff}s.")

    def stop(self) -> None:
        """ Stop provisioned guest """
        super().stop()
        self._virsh('shutdown', self.instance_name)
        self.info('guest', 'stopped', 'green')

    def remove(self) -> None:
        """ Remove the guest (disk cleanup) """
        if self.guest_state != "shut off":
            self._virsh(
                'destroy',
                self.instance_name,
            )
            for i in range(10):
                if self.guest_state == "shut off":
                    break
                time.sleep(1)
        self._virsh(
            'undefine',
            '--remove-all-storage',
            '--nvram',
            '--tpm',
            self.instance_name,
        )
        self.info('guest', 'removed', 'green')

    def reboot(self,
               hard: bool = False,
               command: Optional[Union[Command, ShellScript]] = None,
               timeout: Optional[int] = None,
               tick: float = tmt.utils.DEFAULT_WAIT_TICK,
               tick_increase: float = tmt.utils.DEFAULT_WAIT_TICK_INCREASE) -> bool:
        """ Reboot the guest, return True if successful """
        # Use custom reboot command if provided
        if command:
            return super().reboot(hard=hard, command=command)
        if not self._instance:
            raise tmt.utils.ProvisionError("No instance initialized.")
        # TODO run virsh reboot(soft)/reset(hard)
        return self.reconnect(timeout=timeout)


@tmt.steps.provides_method('diana')
class ProvisionDiana(tmt.steps.provision.ProvisionPlugin):
    """
    Libvirt virtual machine using virt-install and virsh

    Minimal config which uses the provided location consumable by virt-install:

        provision:
            how: diana
            location: http://...

    Here's a full config example:

        provision:
            how: diana
            location: http://...
            hardware:
                memory: 2 GB
    """

    _data_class = ProvisionDianaData
    _guest_class = GuestDiana

    # Guest instance
    _guest = None

    def go(self) -> None:
        """ Provision the libvirt instance """
        super().go()

        # FIXME copied from Testcloud plugin
        data = DianaGuestData(**{
            key: self.get(key)
            # SIM118: Use `{key} in {dict}` instead of `{key} in {dict}.keys()`.
            # "Type[TestcloudGuestData]" has no attribute "__iter__" (not iterable)
            for key in DianaGuestData.keys()  # noqa: SIM118
            })

        data.show(verbose=self.get('verbose'), logger=self._logger)

        # Create a new GuestDiana instance and start it
        self._guest = GuestDiana(
            logger=self._logger,
            data=data,
            name=self.name,
            parent=self.step)
        self._guest.start()

    def guest(self) -> Optional[tmt.Guest]:
        """ Return the provisioned guest """
        return self._guest
