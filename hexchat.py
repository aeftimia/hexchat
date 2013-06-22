#!/usr/bin/env python
import asyncore
import logging
import socket
import sleekxmpp
import sys
import base64
import time
import threading
import xml.etree.cElementTree as ElementTree
import math
import sleekxmpp.xmlstream.handler.callback as callback
import sleekxmpp.xmlstream.matcher.stanzapath as stanzapath

import queue
import copy

# Python versions before 3.0 do not use UTF-8 encoding
# by default. To ensure that Unicode is handled properly
# throughout SleekXMPP, we will set the default encoding
# ourselves to UTF-8.
if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
    sys.maxsize=sys.maxint
else:
    raw_input = input

THROTTLE_RATE=1.0
ASYNCORE_LOOP_RATE=0.05
RECV_RATE=2**17*float(ASYNCORE_LOOP_RATE)/THROTTLE_RATE

LOCK=threading.RLock()

class hexchat_disconnect(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'disconnect'
    namespace = 'hexchat:disconnect'
    plugin_attrib = 'disconnect'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port', 'aliases','id'))
    sub_interfaces=interfaces

class hexchat_connect_message(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'connect'
    namespace = 'hexchat:connect_message'
    plugin_attrib = 'connect_message'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port','aliases','maxsize', 'to'))
    sub_interfaces=interfaces

class hexchat_connect_iq(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'connect'
    namespace = 'hexchat:connect_iq'
    plugin_attrib = 'connect_iq'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port','aliases','maxsize'))
    sub_interfaces=interfaces

class hexchat_connect_ack(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'connect_ack'
    namespace = 'hexchat:connect_ack'
    plugin_attrib = 'connect_ack'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port','aliases','response'))
    sub_interfaces=interfaces

class hexchat_packet(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'packet'
    namespace = 'hexchat:packet'
    plugin_attrib = 'packet'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port','aliases','data', 'id'))
    sub_interfaces=interfaces

#construct key from iq
#return key and tuple indicating whether the key
#is in the client_sockets dict
def iq_to_key(iq):
    if len(iq['remote_port'])>len(str(sys.maxsize)) or len(iq['local_port'])>len(str(sys.maxsize)):
        #these ports are way too long
        raise(ValueError)
        
    local_port=int(iq['remote_port'])
    remote_port=int(iq['local_port'])
            
    local_ip=iq['remote_ip']
    remote_ip=iq['local_ip']

    local_address=(local_ip, local_port)
    remote_address=(remote_ip,remote_port)

    aliases=frozenset(iq['aliases'].split(','))
    
    key=(local_address, aliases, remote_address)
    
    return(key)

class bot(sleekxmpp.ClientXMPP):
    def __init__(self, master, jid_password):
        self.master=master
        sleekxmpp.ClientXMPP.__init__(self, *jid_password)
        self.__event_handlers_lock = self.master.event_handlers_lock
        #self.scheduler=self.master.scheduler
        self.event_queue = self.master.event_queue
        #self.send_queue = self.master.send_queue      
      
        # gmail xmpp server is actually at talk.google.com
        if jid_password[0].find("@gmail.com")!=-1:
            self.connect_address = ("talk.google.com", 5222)
        else:
            self.connect_address = None
        #event handlers are sleekxmpp's way of dealing with important xml tags it receives
        #the only unusual event handler here is the one for "message".
        #this is set to get_message and is used to filter data received over the chat server
        self.add_event_handler("session_start", lambda event: self.session_start())
        self.add_event_handler("disconnected", lambda event: self.disconnected())

        #MUC
        #self.register_plugin('xep_0045')
            
        #these handle the custom iq stanzas
        self.register_handler(callback.Callback('Hexchat Connection Handler',stanzapath.StanzaPath('iq@type=set/connect_iq'),self.master.connect_iq_handler))
        self.register_handler(callback.Callback('Hexchat Message Handler',stanzapath.StanzaPath('message@type=chat/connect_message'),self.master.connect_message_handler))
        self.register_handler(callback.Callback('Hexchat Message Handler',stanzapath.StanzaPath('iq@type=result/connect_ack'),self.master.connect_ack_handler))
        self.register_handler(callback.Callback('Hexchat Disconnection Handler',stanzapath.StanzaPath('iq@type=set/disconnect'),self.master.disconnect_handler))
        self.register_handler(callback.Callback('Hexchat Data Handler',stanzapath.StanzaPath('iq@type=set/packet'),self.master.data_handler))
        self.register_handler(callback.Callback('IQ Error Handler',stanzapath.StanzaPath('iq@type=error'),self.master.error_handler))
                  
    ### session management mathods:

    def session_start(self):
        """Called when the bot connects and establishes a session with the XMPP server."""
        
        # XMPP spec says that we should broadcast our presence when we connect.
        self.send_presence()
        #self.plugin['xep_0045'].joinMUC(self.master.room, self.boundjid.user)

    def disconnected(self):
        """Called when the bot disconnects from the XMPP server.
        Try to reconnect.
        """

        logging.warning("XMPP chat server disconnected")
        logging.debug("Trying to reconnect")
        if self.connect(self.connect_address):
            logging.debug("connection reestabilshed")
        else:
            raise(Exception(self.boundjid.bare+" could not connect"))            

"""this class exchanges data between tcp sockets and xmpp servers."""
class master():
    def __init__(self, jid_passwords):
        """
        Initialize a hexchat XMPP bot. Also connect to the XMPP server.

        'jid' is the login username@chatserver
        'password' is the password to login with
        """

        #self.room=room

        # <local_address> => <listening_socket> dictionary,
        # where 'local_address' is an IP:PORT string with the locallistening address,
        # and 'listening_socket' is the socket that listens and accepts connections on that address.
        self.server_sockets={}

        # <connection_id> => <xmpp_socket> dictionary,
        # where 'connection_id' is a tuple of the form:
        # (bound ip:port on client, xmpp username of server, ip:port server should forward data to)
        # and 'xmpp_socket' is a sleekxmpp socket that speaks to the XMPP bot on the other side.
        self.client_sockets={}

        #pending connections
        self.pending_connections={}

        #peer's resources
        self.peer_resources={}
               
        #initialize the other sleekxmpp clients.
        self.event_handlers_lock=threading.Lock()
        self.event_queue=queue.Queue()
        self.bots=[]
        for jid_password in jid_passwords:
            self.bots.append(bot(self, jid_password))

        for index in range(len(self.bots)):
            if self.bots[index].connect(self.bots[index].connect_address):
                self.bots[index].process()
            else:
                raise(Exception(self.bots[index].boundjid.bare+" could not connect")) 

        self.bot_index=0

    #turn local address and remote address into xml stanzas in the given element tree
    def format_header(self, local_address, remote_address, xml):       
        local_ip_stanza=ElementTree.Element("local_ip")
        local_ip_stanza.text=local_address[0]
        xml.append(local_ip_stanza)      
              
        local_port_stanza=ElementTree.Element("local_port")
        local_port_stanza.text=str(local_address[1])
        xml.append(local_port_stanza)
        
        remote_ip_stanza=ElementTree.Element("remote_ip")
        remote_ip_stanza.text=local_address[0]
        xml.append(remote_ip_stanza)

        remote_port_stanza=ElementTree.Element("remote_port")
        remote_port_stanza.text=str(remote_address[1])
        xml.append(remote_port_stanza)

        aliases_stanza=ElementTree.Element("aliases")
        aliases_stanza.text=",".join(map(lambda bot: bot.boundjid.full, self.bots))
        xml.append(aliases_stanza)
        
        return(xml)
            
    #incomming xml handlers

    def error_handler(self, iq):
        pass

    def connect_iq_handler(self, iq):
        try:
            key=iq_to_key(iq['connect_iq'])
        except ValueError:
            logging.warn('received bad port')
            return()

        if key in self.client_sockets:
            logging.warn("connection request received from a connected socket")   
            return()
            
        try:
            peer_maxsize=int(iq['connect_iq']['maxsize'])
        except ValueError:
            logging.warn("connection request received with bad maxsize value") 
            return()
                
        self.initiate_connection(*key)
        if key in self.client_sockets:
            self.client_sockets[key].peer_maxsize=peer_maxsize
            self.client_sockets[key].aliases=iq['connect_iq']['aliases'].split(',')

    def connect_message_handler(self, msg):
        if not msg['connect_message']['to'] in map(lambda bot: bot.boundjid.bare, self.bots):
            return()
            
        try:
            key=iq_to_key(msg['connect_message'])
        except ValueError:
            logging.warn('received bad port')
            return()

        if key in self.client_sockets:
            logging.warn("connection request received from a connected socket")   
            return()
            
        try:
            peer_maxsize=int(msg['connect_message']['maxsize'])
        except ValueError:
            logging.warn("connection request received with bad maxsize value") 
            return()
                
        self.initiate_connection(*key)
        if key in self.client_sockets:
            self.client_sockets[key].peer_maxsize=peer_maxsize
            self.client_sockets[key].aliases=msg['connect_message']['aliases'].split(',')            

    def disconnect_handler(self, iq):
        """Handles incoming xmpp iqs for disconnections"""
        try:
            key=iq_to_key(iq['disconnect'])
        except ValueError:
            logging.warn('received bad port')
            return()

        if not key in self.client_sockets:
            logging.warn("%s:%d" % key[2] + " seemed to forge a disconnect to %s:%d." % key[0])
            return()
            
        #client wants to disconnect 
        if not self.client_sockets[key].incomming_messages:
            logging.debug("%s:%d" % key[0] + " disconnected from %s:%d." % key[2])
            self.delete_socket(key)     
                 
        try:
            iq_id=int(iq['disconnect']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.delete_socket(key) 
            return()

        id_diff=iq_id-self.client_sockets[key].last_id_received
        if id_diff<0 and id_diff>-self.client_sockets[key].peer_maxsize/2.:
            logging.warn("received redundant message")
            return()

        id_diff=id_diff%self.client_sockets[key].peer_maxsize
        while id_diff>=len(self.client_sockets[key].incomming_messages):
            self.client_sockets[key].incomming_messages.append(None)

        self.client_sockets[key].incomming_messages[id_diff]="disconnect"
            
                    
    def data_handler(self, iq):
        """Handles incoming xmpp iqs for data"""
        try:
            key=iq_to_key(iq['packet'])
        except ValueError:
            logging.warn('received bad port')
            return()
            
        if not key in self.client_sockets:
            #this is most likely caused by one socket disconnecting
            #at the same time that the other socket tried to send data
            #the data arrives before the other socket gets the disconnect message
            #As far as I can tell, this is due to an inherent flaw
            #in trying to pipe TCP data over a chat server
            #Furthermore, it would seem the only way to solve the problem
            #is with raw sockets
            
            #if there was no data, then it was probably
            #just a blank packet sent during
            #the disconnect process
            if iq['packet']['data']:
                logging.warn("%s:%d received data from " % key[0] + "%s:%d, but is not connected." % key[2])
                #this could be because the disconnect signal was dropped by the chat server
                #send a disconnect again
                self.send_disconnect(key)
            return()

        try:
            iq_id=int(iq['packet']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.delete_socket(key) 
            return()

        id_diff=(iq_id-self.client_sockets[key].last_id_received)%self.client_sockets[key].peer_maxsize
        if id_diff<0 and id_diff>-self.client_sockets[key].peer_maxsize/2.:
            logging.warn("recived redundant message")
            return()

        try:
            #extract data, ignoring bytes we already received
            data=base64.b64decode(iq['packet']['data'].encode("UTF-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            logging.warn("%s:%d received invalid data from " % key[0] + "%s:%d. Silently disconnecting." % key[2])
            #bad data can only mean trouble
            #silently disconnect
            self.delete_socket(key)
            return()
        
        logging.debug("%s:%d received data from " % key[0] + "%s:%d" % key[2])


        while id_diff>=len(self.client_sockets[key].incomming_messages):
            self.client_sockets[key].incomming_messages.append(None)

        self.client_sockets[key].incomming_messages[id_diff]=data

        try:
            while self.client_sockets[key].incomming_messages and self.client_sockets[key].incomming_messages[0]!=None:
                data=self.client_sockets[key].incomming_messages[0]
                if data=="disconnect":
                    self.delete_socket(key)
                    break
                self.client_sockets[key].incomming_messages=self.client_sockets[key].incomming_messages[1:]
                self.client_sockets[key].last_id_received=(self.client_sockets[key].last_id_received+1)%self.client_sockets[key].peer_maxsize
                while data:   
                    data=data[self.client_sockets[key].send(data):]
        except KeyError:
            return()
           
    def connect_ack_handler(self, iq):
        try:
            key=iq_to_key(iq['connect_ack'])
        except ValueError:
            logging.warn('received bad port')
            return()
            
        if key in self.client_sockets:
            logging.debug("key not found in sockets or pending connections")
            return()

        for alias in key[1]:
            key0=(key[0], sleekxmpp.xmlstream.JID(alias).bare, key[2])
            if key0 in self.pending_connections:
                break
                
        if key0 in self.pending_connections:
            logging.debug("%s:%d received connection result: " % key[0] + iq['connect_ack']['response'] + " from %s:%d" % key[2])
            self.peer_resources[key0[1]]=iq['from']
            if iq['connect_ack']['response']=="failure":
                self.pending_connections[key0].close()
                del(self.pending_connections[key0])
            else:
                try:
                    peer_maxsize=int(iq['connect_ack']['response'])
                except ValueError:
                    logging.warn("bad result received")
                    return()
                self.client_sockets[key] = asyncore.dispatcher(self.pending_connections.pop(key0))
                self.client_sockets[key].peer_maxsize=peer_maxsize
                self.client_sockets[key].aliases=iq['connect_ack']['aliases'].split(',')
                self.initialize_client_socket(key)
        else:
            logging.warn('iq not in pending connections')

    #methods for sending xml

    def send_data(self, key, data):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element('packet'))
        packet.attrib['xmlns']="hexchat:packet"
        
        data_stanza=ElementTree.Element('data')
        data_stanza.text=data
        packet.append(data_stanza)

        id_stanza=ElementTree.Element('id')
        self.client_sockets[key].id=(self.client_sockets[key].id+1)%sys.maxsize
        id_stanza.text=str(self.client_sockets[key].id)
        packet.append(id_stanza)
        
        self.send_iq(packet, key, 'set')

    def send_disconnect(self, key):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("disconnect"))
        packet.attrib['xmlns']="hexchat:disconnect"
        logging.debug("%s:%d" % local_address + " sending disconnect request to %s:%d" % remote_address)

        id_stanza=ElementTree.Element('id')
        if key in self.client_sockets:
            self.client_sockets[key].id=(self.client_sockets[key].id+1)%sys.maxsize
            id_stanza.text=str(self.client_sockets[key].id)
        else:
            id_stanza.text="None"
        packet.append(id_stanza)
        
        self.send_iq(packet, key, 'set')
        
    def send_connect_ack(self, key, response):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("connect_ack"))
        packet.attrib['xmlns']="hexchat:connect_ack"
        response_stanza=ElementTree.Element("response")
        response_stanza.text=response
        packet.append(response_stanza)
        logging.debug("%s:%d" % local_address + " sending result signal to %s:%d" % remote_address)
        self.send_iq(packet, key, 'result')

    def send_connect_iq(self, key):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("connect"))
        maxsize_stanza=ElementTree.Element('maxsize')
        maxsize_stanza.text=str(sys.maxsize)
        packet.append(maxsize_stanza)
        packet.attrib['xmlns']="hexchat:connect_iq"
        logging.debug("%s:%d" % local_address + " sending connect request to %s:%d" % remote_address)
        self.send_iq(packet, key, 'set')
        
    def send_connect_message(self, key):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("connect"))
        packet.attrib['xmlns']="hexchat:connect_message"
        
        maxsize_stanza=ElementTree.Element('maxsize')
        maxsize_stanza.text=str(sys.maxsize)
        packet.append(maxsize_stanza)

        to_stanza=ElementTree.Element('to')
        to_stanza.text=key[1]
        packet.append(to_stanza)
        
        logging.debug("%s:%d" % local_address + " sending connect request to %s:%d" % remote_address)
        self.bot_index=(self.bot_index+1)%len(self.bots)
        bot=self.bots[self.bot_index]
        message=bot.Message()
        message['to']=key[1]
        message['id']='1'
        message['from']=bot.boundjid.full
        message['type']='chat'
        message.append(packet)
        message.send()

    def send_iq(self, packet, key, iq_type):
        self.bot_index=(self.bot_index+1)%len(self.bots)
        bot=self.bots[self.bot_index]
        iq=bot.Iq()
        iq['from']=bot.boundjid.full
        iq['type']=iq_type
        if key in self.client_sockets:
            self.client_sockets[key].alias_index=(self.client_sockets[key].alias_index+1)%len(self.client_sockets[key].aliases)
            iq['to']=self.client_sockets[key].aliases[self.client_sockets[key].alias_index]
        else:
            if type(key[1])==frozenset:
                iq['to']=set(key[1]).pop()
            else:
                iq['to']=key[1]
        iq.append(packet)
        iq.send(False)

    ### Methods for connection/socket creation.

    def initiate_connection(self, local_address, peer, remote_address):
        """Initiate connection to 'local_address' and add the socket to the client sockets map."""
        
        key=(local_address, peer, remote_address)
        try: # connect to the ip:port
            logging.debug("trying to connect to %s:%d" % local_address)
            connected_socket=socket.create_connection(local_address, timeout=2.0)
        except (socket.error, OverflowError, ValueError):
            logging.warning("could not connect to %s:%d" % local_address)
            #if it could not connect, tell the bot on the the other it could not connect
            self.send_connect_ack(key, "failure")
            return()
            
        logging.debug("connecting %s:%d" % remote_address + " to %s:%d" % local_address)
        # attach the socket to the appropriate client_sockets and fix asyncore methods
        self.client_sockets[key] = asyncore.dispatcher(connected_socket)
        self.client_sockets[key].aliases=list(key[1])
        self.initialize_client_socket(key)
        self.send_connect_ack(key, str(sys.maxsize))

    def initialize_client_socket(self, key):
        #just some asyncore initialization stuff
        self.client_sockets[key].id=0
        self.client_sockets[key].last_id_received=1
        self.client_sockets[key].incomming_messages=[]
        self.client_sockets[key].alias_index=0
        self.client_sockets[key].buffer=b''
        self.client_sockets[key].writable=lambda: False
        self.client_sockets[key].handle_read=lambda: self.handle_read(key)
        self.client_sockets[key].readable=lambda: True
        self.client_sockets[key].close_thread=False
        self.client_sockets[key].handle_close=lambda: self.handle_close(key)
        self.client_sockets[key].running=True
        self.client_sockets[key].thread=threading.Thread(name="check data buffer %d" % hash(key), target=lambda: self.check_data_buffer(key))
        self.client_sockets[key].thread.start()

    def add_server_socket(self, local_address, peer, remote_address):
        """Create a listener and put it in the server_sockets dictionary."""
        self.bot_index=(self.bot_index+1)%len(self.bots)
        self.server_sockets[local_address] = asyncore.dispatcher()
        #just some asyncore initialization stuff
        self.server_sockets[local_address].create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sockets[local_address].writable=lambda: False
        self.server_sockets[local_address].set_reuse_addr()
        self.server_sockets[local_address].bind(local_address)
        self.server_sockets[local_address].handle_accept = lambda: self.handle_accept(local_address, peer, remote_address)
        self.server_sockets[local_address].listen(1023)
        
    ### asyncore callbacks:

    def handle_read(self, key):
        """Called when a TCP socket has stuff to be read from it."""
        try:
            data=self.client_sockets[key].recv(int(RECV_RATE*float(len(self.bots))))
            self.client_sockets[key].buffer+=data
        except KeyError: #socket got deleted while writing to the buffer
            pass

    def handle_accept(self, local_address, peer, remote_address):
        """Called when we have a new incoming connection to one of our listening sockets."""

        connection, local_address = self.server_sockets[local_address].accept()
        
        #add the new connected socket to client_sockets
        #self.add_client_socket(local_address, peer, remote_address, connection)
        #send a connection request to the bot waiting on the other side of the xmpp server
        logging.debug("sending connection request from %s:%d" % local_address + " to %s:%d" % remote_address)
        self.pending_connections[(local_address, peer, remote_address)]=connection
        if peer in self.peer_resources:
            logging.debug("found resource, sending connection request via iq")
            self.send_connect_iq((local_address, self.peer_resources[peer], remote_address))
        else:
            logging.debug("sending connection request via message")
            self.send_connect_message((local_address, peer, remote_address))
        
        
    def handle_close(self, key):
        """Called when the TCP client socket closes."""
        
        if self.client_sockets[key].close_thread:
            return()
        (local_address, remote_address)=(key[0], key[2])
        logging.debug("disconnecting %s:%d from " % local_address +  "%s:%d" % remote_address)
        self.send_disconnect(key)
        self.delete_socket(key)

    def delete_socket(self, key):
        if self.client_sockets[key].close_thread:
            return()
        self.client_sockets[key].close_thread=threading.Thread(name="close %d" % hash(key), target=lambda: self.close_thread(key))
        self.client_sockets[key].close_thread.start()

    def close_thread(self, key):  
        with LOCK:
            if not key in self.client_sockets:
                return()
            self.client_sockets[key].close()
            self.client_sockets[key].running=False
            while self.client_sockets[key].thread.is_alive():
                time.sleep(THROTTLE_RATE/float(len(self.bots)))
            del(self.client_sockets[key])

    #check client sockets for buffered data
    def check_data_buffer(self, key):
        while self.client_sockets[key].running:
            if self.client_sockets[key].buffer:
                data=self.client_sockets[key].buffer
                self.client_sockets[key].buffer=b''
                if data:                    
                    self.send_data(key, base64.b64encode(data).decode("UTF-8"))
            time.sleep(THROTTLE_RATE/float(len(self.bots)))



if __name__ == '__main__':
    logging.basicConfig(filename=sys.argv[2],level=logging.DEBUG)
    
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_disconnect)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_packet)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_connect_iq)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Message, hexchat_connect_message)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_connect_ack)
        
    if sys.argv[1]=="-c":
        #room=sys.argv[3]
        index=3
        username_passwords=[]
        while index<len(sys.argv) and sys.argv[index]!='-s':
            username_passwords.append((sys.argv[index], sys.argv[index+1]))
            index+=2

        master0=master(username_passwords)
        if index<len(sys.argv):
            master0.add_server_socket((sys.argv[index+1],int(sys.argv[index+2])), sys.argv[index+3], (sys.argv[index+4],int(sys.argv[index+5])))
    else:
        if len(sys.argv)!=3:
            raise(Exception("Wrong number of command line arguements"))
        bots={}
        fd=open(sys.argv[1])
        lines=fd.read().splitlines()
        fd.close()
        for line in lines:
            #if the line is of the form username@chatserver:password:
            if line[-1]==":":
                userpass_split=line.find(':')
                try:
                    username=line[:userpass_split]
                    bots[username]=master(username, line[userpass_split+1:-1])
                    continue
                except IndexError:
                    raise(Exception("No password supplied."))
            [local_address, peer, remote_address]=line.split('==>')
            #add a server socket listening for incomming connections
            try:
                local_address=local_address.split(":")
                local_address=(local_address[0],int(local_address[1]))
                remote_address=remote_address.split(":")
                remote_address=(remote_address[0],int(remote_address[1]))
                bots[username].add_server_socket(local_address, peer, remote_address)
            except (OverflowError, socket.error, ValueError) as msg:
                raise(msg)

    #program needs to be kept running on linux
    while True:
        with LOCK:
            asyncore.loop(0.0, True, count=1)
        time.sleep(ASYNCORE_LOOP_RATE)
