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

import eventlet
import socket
import sys
import time

from quantum.agent import rpc as agent_rpc
from quantum.common import config as logging_config
from quantum.common import topics
from quantum.openstack.common import context
from quantum.openstack.common import cfg
from quantum.openstack.common import log as logging
from quantum.openstack.common.rpc import dispatcher
from quantum.plugins.mlnx.agent import utils
from quantum.plugins.mlnx.common import config
from quantum.plugins.mlnx.common import constants

LOG = logging.getLogger(__name__)


class EswitchMngr(object):
    def __init__(self, interface_mappings):
        self.utils = utils.eSwitchUtils()
        self.interface_mappings = interface_mappings
        self.network_map = {}
        self.utils.define_fabric_mappings(interface_mappings)
    
    def get_port_id_by_mac(self,port_mac):
        for network_id, data in self.network_map.iteritems(): 
            for port in data['ports']:
                if port['port_mac'] == port_mac:
                    return port['port_id']
        return port_mac
        
    def get_vnics_mac(self):    
        return set(self.utils.get_attached_vnics().keys()) 
    
    def vnic_port_exists(self,network_id,physical_network, port_mac):  
        if port_mac in self.utils.get_attached_vnics():
            return True
        return False
          
    def remove_network(self,network_id,physical_network,vlan_id):
        if network_id in self.network_map:
            del self.network_map[network_id]
        else:
            LOG.debug(_("Network %s not defined on Agent."), network_id)
    
    def port_down(self,network_id,physical_network,port_mac):
        """
        @note: check  internal network map for port data
            if port exists
                set port to Down
        """
        for network_id, data in self.network_map.iteritems(): 
            for port in data['ports']:
                if port['port_mac'] == port_mac:
                    self.utils.port_down(physical_network,port_mac)
                    break
        else:
            return        
        LOG.info(_('Network %s is not available on this agent'),network_id)
    
    def port_up(self,network_id,network_type,
                physical_network,seg_id,port_id,port_mac):
        """
        @note: update internal network map with port data
               check if vnic defined
                   configure eswitch vport
                   set port to Up
        """
        LOG.debug(_("Connecting port %s"), port_id)
        
        if network_id  not in self.network_map:  
            self.provision_network(port_id,port_mac,
                                   network_id,network_type,
                                   physical_network,seg_id)
        net_map = self.network_map[network_id]
        net_map['ports'].append({'port_id':port_id,'port_mac':port_mac})
        
        if network_type == constants.TYPE_VLAN:
            LOG.info(_('Binding VLAN ID %s to eSwitch for vNIC  mac_address %s'),seg_id, port_mac)
            self.utils.set_port_vlan_id(physical_network,
                                         seg_id,
                                         port_mac)
            self.utils.port_up(physical_network,port_mac)
        elif network_type == constants.TYPE_IB:
            LOG.debug(_('Network Type IB currently not supported'))
        else:
            LOG.error(_('Unsupported network type %s'), network_type)
                

    def port_release(self,port_mac):
        """
    	@note: clear port configuration from eSwitch
    	"""
        for network_id, net_data in self.network_map.iteritems(): 
            for port in net_data['ports']:
                if port['port_mac'] == port_mac:
                    self.utils.port_release(net_data['physical_network'],port['port_mac'])
                    break
        else:
            return 
        
        LOG.info(_('Port_mac %s is not available on this agent'),port_mac)
            
    def provision_network(self, port_id, port_mac,
                            network_id, network_type,
                            physical_network, segmentation_id):
        LOG.info(_("Provisioning network %s"), network_id)
        
        if network_type == constants.TYPE_VLAN:
            #self.utils.define_vlan(physical_network,segmentation_id)
            LOG.debug(_("creating VLAN Netowrk"))
        elif network_type == constants.TYPE_IB:
            LOG.debug(_("currently IB network provisioning is not supported"))
        else:
            LOG.error(_("Cannot provision unknown network type %s for network %s"),
                network_type, network_id)
            return

        data = {
            'physical_network': physical_network,
            'network_type': network_type,
            'ports': [],
            'vlan_id': segmentation_id}
        
        self.network_map[network_id] = data

class MlxEswitchRpcCallbacks():

    # Set RPC API version to 1.0 by default.
    RPC_API_VERSION = '1.0'

    def __init__(self, context,eswitch):
        self.context = context
        self.eswitch = eswitch

    def network_delete(self, context, **kwargs):
        LOG.debug(_("network_delete received"))
        network_id = kwargs.get('network_id')
        if not network_id:
            LOG.warning(_("Invalid Network ID, cannot remove Network"))
        else:    
            LOG.debug(_("Delete network %s"), network_id)
            vlan_id = kwargs.get('vlan_id')
            physical_network = kwargs.get('physical_network')
            self.eswitch.remove_network(network_id,physical_network,vlan_id)

    def port_update(self, context, **kwargs):
        LOG.debug(_("port_update received"))
        port = kwargs.get('port')
        vlan_id = kwargs.get('vlan_id')
        physical_network = kwargs.get('physical_network')
        net_type = kwargs.get('network_type')
        net_id = port['network_id'] 
        if self.eswitch.vnic_port_exists(net_id,physical_network,port['mac_address']):
            if port['admin_state_up']:
                self.eswitch.port_up(net_id, net_type,physical_network, vlan_id,port['id'],port['mac_address'])
            else:
                self.eswitch.port_down(net_id,physical_network,port['mac_address'])
        else:
            LOG.debug(_("No port %s defined on agent."), port['id'])

    def create_rpc_dispatcher(self):
        '''Get the rpc dispatcher for this manager.

        If a manager would like to set an rpc API version, or support more than
        one class as the target of rpc messages, override this method.
        '''
        return dispatcher.RpcDispatcher([self])

class MlxEswitchQuantumAgent(object):
    # Set RPC API version to 1.0 by default.
    RPC_API_VERSION = '1.0'

    def __init__(self,interface_mapping):
        self._polling_interval = cfg.CONF.AGENT.polling_interval
        #self._root_helper = cfg.CONF.AGENT.root_helper
        self._setup_eswitches(interface_mapping)
        self._setup_rpc()

    def _setup_eswitches(self,interface_mapping):
        self.eswitch = EswitchMngr(interface_mapping)
        
    def _setup_rpc(self):
        self.agent_id = 'mlx-agent.%s' % socket.gethostname()
        self.topic = topics.AGENT
        self.plugin_rpc = agent_rpc.PluginApi(topics.PLUGIN)
        # RPC network init
        self.context = context.RequestContext('quantum', 'quantum',
                                              is_admin=False)
        # Handle updates from service
        self.callbacks = MlxEswitchRpcCallbacks(self.context,self.eswitch)
        self.dispatcher = self.callbacks.create_rpc_dispatcher()
        # Define the listening consumers for the agent
        consumers = [[topics.PORT, topics.UPDATE],
                     [topics.NETWORK, topics.DELETE]]
        self.connection = agent_rpc.create_consumers(self.dispatcher,
                                                     self.topic,
                                                     consumers)
  
    def update_ports(self, registered_ports):
        ports = self.eswitch.get_vnics_mac()
        if ports == registered_ports:
            return
        added = ports - registered_ports
        removed = registered_ports - ports
        return {'current': ports,
                'added': added,
                'removed': removed}

    def process_network_ports(self, port_info):
        resync_a = False
        resync_b = False
        if 'added' in port_info:
            print "ports added!"
            resync_a = self.treat_devices_added(port_info['added'])
        if 'removed' in port_info:
            resync_b = self.treat_devices_removed(port_info['removed'])
        # If one of the above opertaions fails => resync with plugin
        return (resync_a | resync_b)
    
    def treat_vif_port(self, port_id,port_mac, 
                       network_id, network_type,
                       physical_network, segmentation_id, 
                       admin_state_up):
        if self.eswitch.vnic_port_exists(network_id,physical_network, port_mac):
            if admin_state_up:
                self.eswitch.port_up(network_id, network_type,physical_network, segmentation_id,port_id,port_mac)
            else:
                self.eswitch.port_down(network_id,physical_network,port_mac)
        else:
            LOG.debug(_("No port %s defined on agent."), port_id)

    def treat_devices_added(self, devices):
        resync = False
        for device in devices:
            LOG.info(_("Adding port with mac %s"),device)
            try:
                dev_details = self.plugin_rpc.get_device_details(
                        self.context,
                        device,
                        self.agent_id)
            except Exception as e:
                LOG.debug(_(
                    "Unable to get device dev_details for device with mac_address %s: %s"),
                    device, e)
                resync = True
                continue
            if 'port_id' in dev_details:
                LOG.info(_("Port %s updated"),device)
                LOG.debug(_("Device details %s"),str(dev_details))
                self.treat_vif_port(
                    dev_details['port_id'],
                    dev_details['port_mac'],
                    dev_details['network_id'],
                    dev_details['network_type'],
                    dev_details['physical_network'],
                    dev_details['vlan_id'],
                    dev_details['admin_state_up'])
            else:
                LOG.debug("Device with mac_address %s not defined on Quantum Plugin", device)
        return resync

    def treat_devices_removed(self, devices):
        resync = False
        for device in devices:
            LOG.info(_("Removing device with mac_address %s"), device)
            try:
                port_id = self.eswitch.get_port_id_by_mac(device)
                dev_details = self.plugin_rpc.update_device_down(self.context,
                                                             port_id,
                                                             self.agent_id)
            except Exception as e:
                LOG.debug(_(
                    "Removing port failed for device %s: %s"),
                    device, e)
                resync = True
                continue           
            LOG.info(_("Port %s updated."),device)
            self.eswitch.port_release(device) 
        return resync

    def daemon_loop(self):
        sync = True
        ports = set()

        while True:
            try:
                start = time.time()
                if sync:
                    LOG.info(_("Agent out of sync with plugin!"))
                    ports.clear()
                    sync = False
                port_info = self.update_ports(ports)
                # notify plugin about port deltas
                if port_info:
                    LOG.debug(_("Agent loop has new devices!"))
                    # If treat devices fails - must resync with plugin
                    sync = self.process_network_ports(port_info)
                    ports = port_info['current']
            except Exception as e:
                LOG.exception(_("Error in agent event loop: %s"), e)
                sync = True
            # sleep till end of polling interval
            elapsed = (time.time() - start)
            if (elapsed < self._polling_interval):
                time.sleep(self._polling_interval - elapsed)
            else:
                LOG.debug(_("Loop iteration exceeded interval "
                            "(%(polling_interval)s vs. %(elapsed)s)"),
                          {'polling_interval': self._polling_interval,
                           'elapsed': elapsed})

def parse_mappings(interface_mappings, mapping):
    physical_network, physical_interface = mapping.split(':')
    interface_mappings[physical_network] = physical_interface
    LOG.debug("physical network %s mapped to physical interface %s" % (physical_network, physical_interface))

def main():
    #eventlet.monkey_patch(os=False, thread=False)
    eventlet.monkey_patch()
    cfg.CONF(args=sys.argv, project='quantum')
    logging_config.setup_logging(cfg.CONF)

    interface_mappings = {}
    for mapping in cfg.CONF.ESWITCH.physical_interface_mappings:
        try:
            parse_mappings(interface_mappings, mapping)
        except ValueError as e:
            LOG.error(_("Parsing physical_interface_mappings failed: %s."
                  " Agent terminated!"), e)
            sys.exit(1)

    LOG.info(_("Interface mappings: %s") % interface_mappings)
    agent = None
    try:
        agent = MlxEswitchQuantumAgent(interface_mappings)
    except Exception, e:
        LOG.error(_("failed to setupeSwitch Daemon with physical_interface_mappings: %s"
                    "Agent terminated!"),e)
        sys.exit(1)

    # Start everything.
    LOG.info(_("Agent initialised successfully, now running... "))
    agent.daemon_loop()
    sys.exit(0)
    
if __name__ == '__main__':
    main()
