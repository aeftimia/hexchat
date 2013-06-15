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
import zlib
import operator
import functools

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

#how many seconds before sending the next packet
#to a given client
THROTTLE_RATE=1.0
ASYNCORE_LOOP_RATE=0.0001

class hexchat_disconnect(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'disconnect'
    namespace = 'hexchat:disconnect'
    plugin_attrib = 'disconnect'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port'))
    sub_interfaces=interfaces

class hexchat_connect(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'connect'
    namespace = 'hexchat:connect'
    plugin_attrib = 'connect'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port'))
    sub_interfaces=interfaces
    
class hexchat_result(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'result'
    namespace = 'hexchat:result'
    plugin_attrib = 'result'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port','response'))
    sub_interfaces=interfaces

class hexchat_packet(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'packet'
    namespace = 'hexchat:packet'
    plugin_attrib = 'packet'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port','data'))
    sub_interfaces=interfaces

#construct key from iq
#return key and tuple indicating whether the key
#is in the client_sockets dict
def iq_to_key(iq, iq_from):
    if len(iq['remote_port'])>len(str(sys.maxsize)) or len(iq['local_port'])>len(str(sys.maxsize)):
        #these ports are way too long
        raise(ValueError)
        
    local_port=int(iq['remote_port'])
    remote_port=int(iq['local_port'])
            
    local_ip=iq['remote_ip']
    remote_ip=iq['local_ip']

    local_address=(local_ip, local_port)
    remote_address=(remote_ip,remote_port)
    
    key=(local_address, iq_from, remote_address)
    
    return(key)

#turn local address and remote address into xml stanzas in the given element tree
def format_header(local_address, remote_address, xml):       
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

        return(xml)
    
"""this class exchanges data between tcp sockets and xmpp servers."""
class bot(sleekxmpp.ClientXMPP):
    def __init__(self, jid, password):
        """
        Initialize a hexchat XMPP bot. Also connect to the XMPP server.

        'jid' is the login username@chatserver
        'password' is the password to login with
        """

        # <local_address> => <listening_socket> dictionary,
        # where 'local_address' is an IP:PORT string with the locallistening address,
        # and 'listening_socket' is the socket that listens and accepts connections on that address.
        self.server_sockets={}

        # <connection_id> => <xmpp_socket> dictionary,
        # where 'connection_id' is a tuple of the form:
        # (bound ip:port on client, xmpp username of server, ip:port server should forward data to)
        # and 'xmpp_socket' is a sleekxmpp socket that speaks to the XMPP bot on the other side.
        self.client_sockets={}

        #map is an opaque "socket map" used by asyncore
        self.map = {}

        #pending connections
        self.pending_connections={}

        #peer's resources
        self.peer_resources={}

        #initialize the sleekxmpp client.
        sleekxmpp.ClientXMPP.__init__(self, jid, password)

        # gmail xmpp server is actually at talk.google.com
        if jid.find("@gmail.com")!=-1:
            self.connect_address = ("talk.google.com", 5222)
        else:
            self.connect_address = None

        #event handlers are sleekxmpp's way of dealing with important xml tags it recieves
        #the only unusual event handler here is the one for "message".
        #this is set to get_message and is used to filter data recieved over the chat server
        self.add_event_handler("session_start", self.session_start)
        self.add_event_handler("disconnected", self.disconnected)
        self.add_event_handler("message", self.message_handler)
        
        #these handle the custom iq stanzas
        self.register_handler(sleekxmpp.xmlstream.handler.callback.Callback('Hexchat Connection Handler',sleekxmpp.xmlstream.matcher.stanzapath.StanzaPath('iq@type=set/connect'),self.connect_handler))
        self.register_handler(sleekxmpp.xmlstream.handler.callback.Callback('Hexchat Disconnection Handler',sleekxmpp.xmlstream.matcher.stanzapath.StanzaPath('iq@type=set/disconnect'),self.disconnect_handler))
        self.register_handler(sleekxmpp.xmlstream.handler.callback.Callback('Hexchat Result Handler',sleekxmpp.xmlstream.matcher.stanzapath.StanzaPath('iq@type=result/result'),self.result_handler))
        self.register_handler(sleekxmpp.xmlstream.handler.callback.Callback('Hexchat Data Handler',sleekxmpp.xmlstream.matcher.stanzapath.StanzaPath('iq@type=set/packet'),self.data_handler))
        #The scheduler is xmpp's multithreaded todo list
        #This line adds asyncore's loop to the todo list
        #It tells the scheduler to evaluate asyncore.loop(0.0, True, self.map, 1)
        self.scheduler.add("asyncore loop", ASYNCORE_LOOP_RATE, asyncore.loop, (0.0, True, self.map, 1), repeat=True)

        # Connect to XMPP server
        if self.connect(self.connect_address):
            self.process()
        else:
            raise(Exception(jid+" could not connect"))       

    ### XMPP handling methods:

    def session_start(self, event):
        """Called when the bot connects and establishes a session with the XMPP server."""

        # XMPP spec says that we should broadcast our presence when we connect.
        self.send_presence()

    def disconnected(self, event):
        """Called when the bot disconnects from the XMPP server.
        Try to reconnect.
        """

        logging.warning("XMPP chat server disconnected")
        logging.debug("Trying to reconnect")
        if self.connect(self.connect_address):
            logging.debug("connection reestabilshed")
        else:
            raise(Exception(jid+" could not connect"))
            
    #incomming xml handlers

    def connect_handler(self, iq):
        try:
            key=iq_to_key(iq['connect'], iq['from'])
        except ValueError:
            logging.warn('recieved bad port')
            return()

        if not key in self.client_sockets:
            self.initiate_connection(*key)
        else:
            logging.warn("connection request recieved from a connected socket")   

    def message_handler(self, message):
        try:
            remote_address=(message['nick']['nick'],int(message['id']))
            local_address=(message['body'], int(message['subject']))
        except ValueError:
            logging.warn('recieved bad port')
            return()

        key=(local_address, message['from'], remote_address)
        if not key in self.client_sockets:
            self.initiate_connection(*key)
        else:
            logging.warn("connection request recieved from a connected socket")  
         
    def disconnect_handler(self, iq):
        """Handles incoming xmpp iqs for disconnections"""
        try:
            key=iq_to_key(iq['disconnect'], iq['from'])
        except ValueError:
            logging.warn('recieved bad port')
            return()

        if key in self.client_sockets:
            #client wants to disconnect 
            logging.debug("%s:%d" % key[0] + " disconnected from %s:%d." % key[2])
            self.delete_socket(key)
        else:
            logging.warn("%s:%d" % key[2] + " seemed to forge a disconnect to %s:%d." % key[0])
                    
    def data_handler(self, iq):
        """Handles incoming xmpp iqs for data"""
        try:
            key=iq_to_key(iq['packet'], iq['from'])
        except ValueError:
            logging.warn('recieved bad port')
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
                logging.warn("%s:%d recieved data from " % key[0] + "%s:%d, but is not connected." % key[2])
                #this could be because the disconnect signal was dropped by the chat server
                #send a disconnect again
                self.send_disconnect(key)
            return()

        try:
            data=zlib.decompress(base64.b64decode(iq['packet']['data'].encode("UTF-8")))
        except (UnicodeDecodeError, TypeError, binascii.Error):
            logging.warn("%s:%d recieved invalid data from " % key[0] + "%s:%d. Silently disconnecting." % key[2])
            #bad data can only mean trouble
            #silently disconnect
            self.delete_socket(key)
            return()

        logging.debug("%s:%d recieved data from " % key[0] + "%s:%d" % key[2] + ":%s" % (iq['packet']['data']))

        while data:
            try:        
                data=data[self.client_sockets[key].send(data):]
            except KeyError:
                return()

        #acknowledge the data was recieved
        logging.debug("%s:%d sending confirmation of id " % key[0] + "%s" % (iq['id']) + " to %s:%d" % key[2])
        self.send_result(key, iq['id'])
           
    def result_handler(self, iq):
        try:
            key=iq_to_key(iq['result'], iq['from'])
        except ValueError:
            logging.warn('recieved bad port')
            return()

        if not key in self.client_sockets:
            key0=(key[0], iq['from'].bare, key[2])
            if key0 in self.pending_connections:
                logging.debug("%s:%d recieved connection result: "  % key[0] + iq['result']['response'] + " from %s:%d" % key[2])
                if not key[1] in self.peer_resources:
                    self.peer_resources[key0[1]]=iq['from']
                if iq['result']['response']=="success":
                    self.client_sockets[key] = asyncore.dispatcher(self.pending_connections.pop(key0), map=self.map)
                    self.initialize_client_socket(key)
                elif iq['result']['response']=="failure":
                    self.pending_connections[key0].close()
                    del(self.pending_connections[key0])
        else:
            try:
                id_diff=(self.client_sockets[key].id-int(iq['result']['response'])) % sys.maxsize
                if id_diff<=len(self.client_sockets[key].cache_lengths):
                    #data has been acknowledged
                    #clear the cache
                    logging.debug("clearing cache")
                    cache_length=functools.reduce(operator.add, self.client_sockets[key].cache_lengths[:id_diff], 0)
                    self.client_sockets[key].cache_data=self.client_sockets[key].cache_data[cache_length:]
                    self.client_sockets[key].cache_lengths=self.client_sockets[key].cache_lengths[id_diff:]
                else:
                    raise(ValueError)
            except ValueError:
                logging.warn("result recieved from invalid id. Recieved %s, looking for %d" % (iq['result']['response'], self.client_sockets[key].id))

    #methods for sending xml

    def send_data(self, key, data):
        (local_address, remote_address)=(key[0], key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element("packet"))
        packet.attrib['xmlns']="hexchat:packet"
        data_stanza=ElementTree.Element("data")
        data_stanza.text=data
        packet.append(data_stanza)
        self.send_iq(packet, key, 'set')

    def send_connect(self, key):
        (local_address, remote_address)=(key[0], key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element("connect"))
        packet.attrib['xmlns']="hexchat:connect"
        logging.debug("%s:%d" % local_address + " sending connect request to %s:%d" % remote_address)
        self.send_iq(packet, key, 'set')

    def send_disconnect(self, key):
        (local_address, remote_address)=(key[0], key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element("disconnect"))
        packet.attrib['xmlns']="hexchat:disconnect"
        logging.debug("%s:%d" % local_address + " sending disconnect request to %s:%d" % remote_address)
        self.send_iq(packet, key, 'set')
        
    def send_result(self, key, response):
        (local_address, remote_address)=(key[0], key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element("result"))
        packet.attrib['xmlns']="hexchat:result"
        response_stanza=ElementTree.Element("response")
        response_stanza.text=response
        packet.append(response_stanza)
        logging.debug("%s:%d" % local_address + " sending result signal to %s:%d" % remote_address)
        self.send_iq(packet, key, 'result')
        
    def request_resource(self, local_address, peer, remote_address):
        message=self.Message()
        message['to']=peer
        message['from']=self.boundjid
        
        #hide info in message headers
        message['nick']=local_address[0]
        message['id']=str(local_address[1])
        message['body']=remote_address[0]
        message['subject']=str(remote_address[1])
        message.send()

    def send_iq(self, packet, key, iq_type):
        iq=self.Iq()
        iq['from']=self.boundjid
        iq['to']=key[1]
        iq['type']=iq_type
        if key in self.client_sockets:
            iq['id']=str(self.client_sockets[key].id)
        iq.append(packet)
        iq.send(False)

    ### Methods for connection/socket creation.

    def initiate_connection(self, local_address, peer, remote_address):
        """Initiate connection to 'local_address' and add the socket to the client sockets map."""
        
        key=(local_address, peer, remote_address)
        try: # connect to the ip:port
            connected_socket=socket.create_connection(local_address)
        except (socket.error, OverflowError, ValueError):
            logging.warning("could not connect to %s:%d" % local_address)
            #if it could not connect, tell the bot on the the other it could not connect
            self.send_result(key, "failure")
            return()
            
        logging.debug("connecting %s:%d" % remote_address + " to %s:%d" % local_address)
        # attach the socket to the appropriate client_sockets and fix asyncore methods
        self.client_sockets[key] = asyncore.dispatcher(connected_socket, map=self.map)
        self.initialize_client_socket(key)
        self.send_result(key, "success")

    def initialize_client_socket(self, key):
        #just some asyncore initialization stuff
        self.client_sockets[key].buffer=b''
        self.client_sockets[key].id=0
        self.client_sockets[key].cache_lengths=[]
        self.client_sockets[key].cache_data=b''
        self.client_sockets[key].writable=lambda: False
        self.client_sockets[key].readable=lambda: True
        self.client_sockets[key].handle_read=lambda: self.handle_read(key)
        self.client_sockets[key].handle_close=lambda: self.handle_close(key)
        self.scheduler.add("check buffer %d" % hash(key), THROTTLE_RATE, lambda: self.check_buffer(key), (), repeat=True)

    def add_server_socket(self, local_address, peer, remote_address):
        """Create a listener and put it in the server_sockets dictionary."""
        
        self.server_sockets[local_address] = asyncore.dispatcher(map=self.map)
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
        self.client_sockets[key].buffer+=self.client_sockets[key].recv(8192)

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
            self.send_connect((local_address, self.peer_resources[peer], remote_address))
        else:
            logging.debug("requesting resource")
            self.request_resource(local_address, peer, remote_address)
        
        
    def handle_close(self, key):
        """Called when the TCP client socket closes."""
        if not key in self.client_sockets:
            return()
            
        #send a disconnection request to the bot waiting on the other side of the xmpp server
        (local_address, remote_address)=(key[0], key[2])
        logging.debug("disconnecting %s:%d from " % local_address +  "%s:%d" % remote_address)
        self.send_disconnect(key)
        self.delete_socket(key)

    def delete_socket(self, key):
        self.scheduler.remove("check buffer %d" % hash(key))
        self.client_sockets[key].close()
        del(self.client_sockets[key])

    #check client sockets for buffered data
    def check_buffer(self, key):
        try:
            if self.client_sockets[key].buffer or self.client_sockets[key].cache_data:
                data=self.client_sockets[key].buffer
                self.client_sockets[key].id=(self.client_sockets[key].id+1)%sys.maxsize
                self.send_data(key, base64.b64encode(zlib.compress(self.client_sockets[key].cache_data+data, 9)).decode("UTF-8"))
                self.client_sockets[key].buffer=self.client_sockets[key].buffer[len(data):]
                self.client_sockets[key].cache_data+=data
                self.client_sockets[key].cache_lengths.append(len(data))
        except KeyError:
            pass

if __name__ == '__main__':
    logging.basicConfig(filename=sys.argv[2],level=logging.DEBUG)
    
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_disconnect)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_result)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_connect)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_packet)
    
    if sys.argv[1]=="-c":
        if not len(sys.argv) in (5,10):
            raise(Exception("Wrong number of command line arguements"))
        else:
            username=sys.argv[3]
            password=sys.argv[4]
            bot0=bot(username, password)
            if len(sys.argv)==10:
                bot0.add_server_socket((sys.argv[5],int(sys.argv[6])), sys.argv[7], (sys.argv[8],int(sys.argv[9])))
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
                    bots[username]=bot(username, line[userpass_split+1:-1])
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
        time.sleep(1)
