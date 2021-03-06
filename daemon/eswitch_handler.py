# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013 Mellanox Technologies, Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from nova.openstack.common import log as logging
from utils import pci_utils
from utils.command_utils import execute
from db import eswitch_db
from resource_mngr import ResourceManager 

LOG = logging.getLogger('mlnx_daemon')

class eSwitchHandler(object):
    def __init__(self,fabrics=None):
        self.eswitches = {}
        self.pci_utils = pci_utils.pciUtils()
        self.rm = ResourceManager()
        self.devices = set()
        if fabrics:
            self.add_fabrics(fabrics)
    
    def add_fabrics(self,fabrics):
        for fabric, pf in fabrics:
            self.eswitches[fabric] = eswitch_db.eSwitchDB()
            self._add_fabric(fabric,pf)
        self.sync_devices()  
          
    def sync_devices(self):
        devices = self.rm.scan_attached_devices()
        added_devs = set(devices['direct'])-self.devices
        removed_devs = self.devices-set(devices['direct'])      
        self._treat_added_devices(added_devs)
        self._treat_removed_devices(removed_devs)
        self.devices = set(devices['direct'])

    def _add_fabric(self,fabric,pf):
        self.rm.add_fabric(fabric,pf)
        vfs = self.rm.get_free_vfs(fabric)
        eths = self.rm.get_free_eths(fabric)
        for vf in vfs:
            self.eswitches[fabric].create_port(vf, 'hostdev')
        for eth in eths:
            self.eswitches[fabric].create_port(eth, 'direct')

    def _treat_added_devices(self, devices):
        for dev, mac, fabric in devices:
            if fabric:
                self.rm.allocate_device(fabric, dev_type='direct', dev=dev)
                self.eswitches[fabric].attach_vnic(port_name=dev, device_id=None, vnic_mac=mac)
            else:
                LOG.debug("No Fabric defined for device %s", dev)
                
    def _treat_removed_devices(self,devices):
        for dev, mac in devices:
            fabric = self.rm.get_fabric_for_dev(dev)
            if fabric:
                self.rm.deallocate_device(fabric, dev_type='direct', dev=dev)
                self.eswitches[fabric].detach_vnic(vnic_mac=mac)
            else:
                LOG.debug("No Fabric defined for device %s", dev)


#-------------------------------------------------
#  requests handling
#-------------------------------------------------

    def set_fabric_mapping(self, fabric, interface):
        dev = self.rm.get_fabric_for_dev(interface)
        if not dev:
            fabrics = [(fabric, interface)]
            self.add_fabrics(fabrics)
            dev = interface
        return (fabric, interface)
           
    def get_vnics(self, fabrics):
        vnics = {}
        for fabric in fabrics:
            eswitch = self._get_vswitch_for_fabric(fabric)
            if eswitch:
                vnics_for_eswitch = eswitch.get_attached_vnics()
                vnics.update(vnics_for_eswitch)
            else:
                LOG.error("No eSwitch found for Fabric %s",fabric)
                continue
        LOG.debug("vnics are %s",vnics)
        return vnics  
    
    def create_port(self, fabric, vnic_type, device_id, vnic_mac):
        dev = None
        eswitch = self._get_vswitch_for_fabric(fabric)
        if eswitch:
            dev = eswitch.get_dev_for_vnic(vnic_mac)
            if not dev:
                dev = self.rm.allocate_device(fabric, vnic_type)
                if dev:
                    if not eswitch.attach_vnic(dev, device_id, vnic_mac):
                        self.rm.deallocate_device(fabric,vnic_type,dev)
                        dev = None
        else:
            LOG.error("No eSwitch found for Fabric %s",fabric)
        return dev
         
    def delete_port(self, fabric, vnic_mac):
        dev = None
        eswitch = self._get_vswitch_for_fabric(fabric)
        if eswitch:
            dev = eswitch.detach_vnic(vnic_mac)
            if dev:
                dev_type = eswitch.get_dev_type(dev)
                self.rm.deallocate_device(fabric,dev_type,dev)
        else:
            LOG.error("No eSwitch found for Fabric %s",fabric)
        return dev  

    def port_release(self, fabric, vnic_mac):
        ret = self.set_vlan(fabric, vnic_mac, 0)   
        return ret
    
    def set_vlan(self, fabric, vnic_mac, vlan):
        eswitch = self._get_vswitch_for_fabric(fabric)
        if eswitch:
            eswitch.set_vlan(vnic_mac, vlan)
            dev = eswitch.get_dev_for_vnic(vnic_mac)
            if dev:
                vnic_type = eswitch.get_port_type(dev)
                pf = self.rm.get_fabric_pf(fabric)
                vf_index = self.pci_utils.get_vf_index(dev, vnic_type)
                if pf and vf_index:
                    try:
                        self._config_vlan_priority(pf, vf_index, dev, vlan)
                        return True
                    except RuntimeError:
                        LOG.error('Set VLAN operation failed')    
                else:
                    LOG.error('Invalid VF/PF index for device %s',dev)         
        return False
        
    def _get_vswitch_for_fabric(self, fabric):
        if fabric in self.eswitches:
            return self.eswitches[fabric]
        else:
            return 
        
    def _config_vlan_priority(self, pf, vf_index, dev, vlan, priority='0'):
        cmd = ['ip', 'link', 'set', dev, 'down']       
        execute(cmd, root_helper='sudo')
    
        cmd = ['ip', 'link','set',pf, 'vf', vf_index , 'vlan', vlan, 'qos', priority]
        execute(cmd, root_helper='sudo')
    
        cmd = ['ip', 'link', 'set', dev, 'up']       
        execute(cmd, root_helper='sudo')


    
