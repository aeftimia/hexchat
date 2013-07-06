import time

import xml.etree.cElementTree as ElementTree
from sleekxmpp.xmlstream import tostring
from sleekxmpp.stanza import Message, Iq

KARMA_RESET=10.0 #seconds
THROUGHPUT=4.9*10**3 #bytes/second

MAX_ID=2**32-1
MAX_DB_SIZE=2**22 #bytes
MAX_SIZE=2**15 #bytes

TIMEOUT=300.0 #seconds before closing a socket if it has not gotten a connect_ack
CHECK_RATE=0.1 #seconds to check for a condition

CONNECT_TIMEOUT=1.0 #time to wait to try to connect a socket to the requested ip:port
PENDING_DISCONNECT_TIMEOUT=2.0 #time to wait for the chat server to send an error
SELECT_TIMEOUT=0.0
SELECT_LOOP_RATE=0.01 #rate to poll sockets

MAX_THROUGHPUT=100*10**3 #bytes/second
MIN_THROUGHPUT=64*10**3 #bytes/second

MAX_ALIASES=MAX_THROUGHPUT/THROUGHPUT
MIN_ALIASES=MIN_THROUGHPUT/THROUGHPUT

#construct key from msg
def msg_to_key(msg, aliases):
    if len(msg['remote_port'])>6 or len(msg['local_port'])>6:
        #these ports are way too long
        raise(ValueError)
        
    local_port=int(msg['remote_port'])
    remote_port=int(msg['local_port'])
            
    local_ip=msg['remote_ip']
    remote_ip=msg['local_ip']

    local_address=(local_ip, local_port)
    remote_address=(remote_ip,remote_port)
    
    key=(local_address, aliases, remote_address)
    
    return key

def alias_decode(msg, root):
    element_tree=msg.xml
    xml_dict=elementtree_to_dict(element_tree)
    root_dict=xml_dict[root][0]
    if not 'aliases' in root_dict:
        return set()
    alias_dict=root_dict['aliases'][0]

    aliases=set()
    for server in alias_dict:
        for user in alias_dict[server][0]:
            resources=alias_dict[server][0][user][0].split(",")
            for resource in resources:
                aliases.add("%s@%s/%s" % (user, server, resource))
    
    return frozenset(aliases)

#modified from http://codereview.stackexchange.com/questions/10400/convert-elementtree-to-dict
def elementtree_to_dict(element):
    node = dict()

    text = getattr(element, 'text', None)
    if text is not None:
        return text

    nodes = {}
    for child in element: # element's children
        tag = get_tag(child)
        subdict=elementtree_to_dict(child)
        if tag in nodes:
            nodes[tag].append(subdict)
        else:
            nodes[tag]=[subdict]

    return nodes

def get_tag(element):
    if element.tag[0] == "{":
        return element.tag[1:].partition("}")[2]
    else:
        return elem.tag

#turn local address and remote address into xml stanzas in the given element tree
def format_header(local_address, remote_address, xml):       
    local_ip_stanza=ElementTree.Element("local_ip")
    local_ip_stanza.text=local_address[0]
    xml.append(local_ip_stanza)      
              
    local_port_stanza=ElementTree.Element("local_port")
    local_port_stanza.text=str(local_address[1])
    xml.append(local_port_stanza)
        
    remote_ip_stanza=ElementTree.Element("remote_ip")
    remote_ip_stanza.text=remote_address[0]
    xml.append(remote_ip_stanza)

    remote_port_stanza=ElementTree.Element("remote_port")
    remote_port_stanza.text=str(remote_address[1])
    xml.append(remote_port_stanza)
        
    return xml

def karma_better(karma_vars1, karma_vars2):
    now=time.time()    
    return karma_vars1[1]/(now-karma_vars1[0])<karma_vars2[1]/(now-karma_vars2[0])


def send_thread(str_data, bot):
    sleep_seconds=len(str_data)/THROUGHPUT
    with bot.send_lock:
        then=time.time()
        bot.send_raw(str_data, now=True)
        dtime=time.time()-then
        if dtime<sleep_seconds:
            time.sleep(sleep_seconds-dtime)

class Peer_Resource_DB():

    def __init__(self):
        self.dict={}
        self.index={}
        
    def add(self, key, resource):
        if key in self.dict:
            if resource in self.dict[key]:
                return
            else:
                self.dict[key].append(resource)
        else:
            self.dict[key]=[resource]
            self.index[key]=0

    def __contains__(self, key):
        return key in self.dict

    def __getitem__(self, key):
        resources=self.dict[key]
        index=self.index[key]
        resource=resources[index]
        self.index[key]=(self.index[key]+1)%len(resources)
        return resource

    def remove(self, key, resource):
        self.dict[key].remove(resource)
        if not self.dict[key]:
            del(self.dict[key])
            del(self.index[key])
        else:
            self.index[key]=self.index[key]%len(self.dict[key])