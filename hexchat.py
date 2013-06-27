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
import sleekxmpp.xmlstream.handler.callback as callback
import sleekxmpp.xmlstream.matcher.stanzapath as stanzapath

# Python versions before 3.0 do not use UTF-8 encoding
# by default. To ensure that Unicode is handled properly
# throughout SleekXMPP, we will set the default encoding
# ourselves to UTF-8.
if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input

THROTTLE_RATE=1.0
RECV_RATE=2**16
MAX_ID=2**32-1
MAX_ID_DIFF=100

class hexchat_disconnect(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'disconnect'
    namespace = 'hexchat:disconnect'
    plugin_attrib = 'disconnect'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port', 'aliases','id'))
    sub_interfaces=interfaces

class hexchat_connect(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'connect'
    namespace = 'hexchat:connect'
    plugin_attrib = 'connect'
    interfaces = set(('local_ip','local_port','remote_ip','remote_port','aliases'))
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
    if len(iq['remote_port'])>6 or len(iq['local_port'])>6:
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
        #self.scheduler=self.master.scheduler
        #self.event_queue = self.master.event_queue
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

        self.register_plugin('xep_0199') # Ping

        if self.connect(self.connect_address):
            self.process()
        else:
            raise(Exception(self.bots[index].boundjid.bare+" could not connect"))

    def register_hexchat_handlers(self):
        #these handle the custom iq stanzas
        self.register_handler(callback.Callback('Connection Handler',stanzapath.StanzaPath('iq@type=set/connect'),self.master.connect_handler))
        self.register_handler(callback.Callback('Message Handler',stanzapath.StanzaPath('message@type=chat/connect'),self.master.connect_handler))
        self.register_handler(callback.Callback('Connect Ack Handler',stanzapath.StanzaPath('iq@type=result/connect_ack'),self.master.connect_ack_handler))
        self.register_handler(callback.Callback('Disconnection Handler',stanzapath.StanzaPath('iq@type=set/disconnect'),self.master.disconnect_handler))
        self.register_handler(callback.Callback('Data Handler',stanzapath.StanzaPath('iq@type=set/packet'),self.master.data_handler))
        
        self.register_handler(callback.Callback('IQ Error Handler',stanzapath.StanzaPath('iq@type=error/error'), self.master.error_handler))
        self.register_handler(callback.Callback('Message Error Handler',stanzapath.StanzaPath('message@type=error/error'),self.master.error_handler))
                  
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


class client_socket(asyncore.dispatcher):
    def __init__(self, master, key, socket):
        self.master=master
        self.key=key
        self.aliases=list(key[1])
        self.id=0
        self.last_id_received=0
        self.incomming_messages=[]
        self.alias_index=0
        self.buffer=b''
        self.running=True
        self.running_lock=threading.RLock()
        self.alias_lock=threading.Lock()
        self.id_lock=threading.Lock()
        socket.setblocking(1)
        self.socket=socket

    def run(self):
        threading.Thread(name="read socket %d" % hash(self.key), target=lambda: self.read_socket()).start()

    def get_alias(self):
        with self.alias_lock:
            alias=self.aliases[self.alias_index%len(self.aliases)]
            self.alias_index=(self.alias_index+1)%len(self.aliases)
            return(alias)

    #check client sockets for buffered data
    def read_socket(self):
        while True:
            data=self.recv(RECV_RATE)
            if data: 
                self.master.send_data(self.key, base64.b64encode(data).decode("UTF-8"), self.id, self.get_alias())
                with self.id_lock:
                    self.id=(self.id+1)%MAX_ID
            else:
                with self.running_lock:
                    if self.running:
                        self.master.send_disconnect(self.key, self.id, self.get_alias())
                        self._handle_close()
                return()
            time.sleep(THROTTLE_RATE/float(len(self.master.bots)))

    def buffer_message(self, iq_id, data):
        threading.Thread(name="%d buffer message %d" % (hash(self.key), iq_id), target=lambda: self.buffer_message_thread(iq_id, data)).start()
            
    def buffer_message_thread(self, iq_id, data):
        with self.running_lock:
            if not self.running:
                return()
                
            raw_id_diff=(iq_id-self.last_id_received)
            id_diff=raw_id_diff%MAX_ID
            if raw_id_diff<0 and raw_id_diff>-MAX_ID/2. or id_diff>MAX_ID_DIFF:
                logging.warn("received redundant message or too many messages in buffer. Disconnecting")
                self.master.send_disconnect(self.key, self.id, self.get_alias())
                self._handle_close()
                return()

            logging.debug("%s:%d received data from " % self.key[0] + "%s:%d" % self.key[2])
            while id_diff>=len(self.incomming_messages):
                self.incomming_messages.append(None)

            self.incomming_messages[id_diff]=data
            logging.debug("%s:%d looking for id:"%self.key[0]+str(self.last_id_received))
            while self.incomming_messages and self.incomming_messages[0]!=None:
                data=self.incomming_messages.pop(0)
                if data=="disconnect":
                    self._handle_close()
                    return()
                self.last_id_received=(self.last_id_received+1)%MAX_ID
                logging.debug("%s:%d now looking for id:"%self.key[0]+str(self.last_id_received))
                while data:   
                    data=data[self.send(data):]

    def _handle_close(self):
        """Called when the TCP client socket closes."""
        with self.running_lock:
            if not self.running:
                return()
            self.running=False
            (local_address, remote_address)=(self.key[0], self.key[2])
            logging.debug("disconnecting %s:%d from " % local_address +  "%s:%d" % remote_address)
            self.close()
            self.master.delete_socket(self.key)

    def close(self):
        #self.connected = False
        #self.accepting = False
        #self.del_channel()
        try:
            self.socket.close()
        except socket.error as why:
            if why.args[0] not in (asyncore.ENOTCONN, asyncore.EBADF):
                raise

class server_socket(asyncore.dispatcher):
    def __init__(self, master, local_address, peer, remote_address):
        self.master=master
        self.local_address=local_address
        self.peer=peer
        self.remote_address=remote_address
        self.socket=socket
        self.socket=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setblocking(1)
        self.set_reuse_addr()
        self.bind(local_address)
        self.listen(1023)
        self.run_thread=threading.Thread(name="accept %d" % hash(local_address), target=lambda: self.accept_thread())

    def accept_thread(self):
        while True:
            connection, local_address = self.accept()
            with self.master.pending_connections_lock:
                logging.debug("sending connection request from %s:%d" % local_address + " to %s:%d" % self.remote_address)
                self.master.pending_connections[(local_address, self.peer, self.remote_address)]=connection
                if self.peer in self.master.peer_resources:
                    logging.debug("found resource, sending connection request via iq")
                    self.master.send_connect_iq((local_address, self.master.peer_resources[self.peer], self.remote_address))
                else:
                    logging.debug("sending connection request via message")
                    self.master.send_connect_message((local_address, self.peer, self.remote_address))       

"""this class exchanges data between tcp sockets and xmpp servers."""
class master():
    def __init__(self, jid_passwords, whitelist):
        """
        Initialize a hexchat XMPP bot. Also connect to the XMPP server.

        'jid' is the login username@chatserver
        'password' is the password to login with
        """

        #whitelist of trusted ip addresses client sockets can connect to
        #if None, can connect to anything
        self.whitelist=whitelist

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

        #locks
        self.client_sockets_lock=threading.Lock()
        self.pending_connections_lock=threading.Lock()
        self.peer_resources_lock=threading.Lock()
        self.bot_lock=threading.Lock()
               
        #initialize the other sleekxmpp clients.
        self.bots=[]
        for jid_password in jid_passwords:
            self.bots.append(bot(self, jid_password))

        self.bot_index=0

        while True in map(lambda bot: bot.boundjid.full==bot.boundjid.bare, self.bots):
            time.sleep(THROTTLE_RATE)

        self.aliases=frozenset(map(lambda bot: bot.boundjid.full, self.bots)) 

        for index in range(len(self.bots)):
            self.bots[index].register_hexchat_handlers()

    def get_bot(self):
        with self.bot_lock:
            bot=self.bots[self.bot_index%len(self.bots)]
            self.bot_index=(self.bot_index+1)%len(self.bots)
            return(bot)

            
    #turn local address and remote address into xml stanzas in the given element tree
    def format_header(self, local_address, remote_address, xml):       
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

        aliases_stanza=ElementTree.Element("aliases")
        aliases_stanza.text=",".join(self.aliases)
        xml.append(aliases_stanza)
        
        return(xml)
            
    #incomming xml handlers

    def error_handler(self, iq):
        with self.peer_resources_lock:
            try:
                del(self.peer_resources[iq['from'].bare])
            except KeyError:
                pass

        with self.pending_connections_lock:
            try:
                self.pending_connections[iq['from'].bare].close()
                del(self.pending_connections[iq['from'].bare])
            except KeyError:
                pass

        with self.client_sockets_lock:
            for key in self.client_sockets:
                if iq['from'].full in self.client_sockets[key].aliases:
                    if len(self.client_sockets[key].aliases)>1:
                        with self.client_sockets[key].alias_lock:
                            self.client_sockets[key].aliases=list(frozenset(self.client_sockets[key].aliases)-frozenset([iq['from'].full]))
                        with self.client_sockets[key].id_lock:
                            self.client_sockets[key].id=(self.client_sockets[key].id-1)%MAX_ID
                    else:
                        self.client_sockets[key]._handle_close()

    def connect_handler(self, msg):          
        try:
            key=iq_to_key(msg['connect'])
        except ValueError:
            logging.warn('received bad port')
            return()

        if key in self.client_sockets:
            logging.warn("connection request received from a connected socket")   
            return()

        self.initiate_connection(key, msg['to'])         

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
        try:
            iq_id=int(iq['disconnect']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.client_sockets[key]._handle_close()
            return()

        self.client_sockets[key].buffer_message(iq_id, "disconnect")
            
                    
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
            return()

        try:
            iq_id=int(iq['packet']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.client_sockets[key]._handle_close()
            return()

        try:
            #extract data, ignoring bytes we already received
            data=base64.b64decode(iq['packet']['data'].encode("UTF-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            logging.warn("%s:%d received invalid data from " % key[0] + "%s:%d. Silently disconnecting." % key[2])
            #bad data can only mean trouble
            #silently disconnect
            self.client_sockets[key]._handle_close()
            return()

        self.client_sockets[key].buffer_message(iq_id, data)
           
    def connect_ack_handler(self, iq):
        try:
            key=iq_to_key(iq['connect_ack'])
        except ValueError:
            logging.warn('received bad port')
            return()

        key0=(key[0], iq['from'].bare, key[2])
        if not key0 in self.pending_connections:
            logging.debug("key not found in sockets or pending connections")
            return()
                
        if not key0 in self.pending_connections:
            logging.warn('iq not in pending connections')
            return()
           
        with self.peer_resources_lock:
            self.peer_resources[key0[1]]=iq['from'].full
            
        with self.pending_connections_lock:
            logging.debug("%s:%d received connection result: " % key[0] + iq['connect_ack']['response'] + " from %s:%d" % key[2])
            if iq['connect_ack']['response']=="failure":
                self.pending_connections[key0].close()
                del(self.pending_connections[key0])
            else:
                self.create_client_socket(key, self.pending_connections.pop(key0))

    #methods for sending xml

    def send_data(self, key, data, iq_id, alias):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element('packet'))
        packet.attrib['xmlns']="hexchat:packet"

        id_stanza=ElementTree.Element('id')
        id_stanza.text=str(iq_id)
        packet.append(id_stanza)
        
        data_stanza=ElementTree.Element('data')
        data_stanza.text=data
        packet.append(data_stanza)
        
        bot=self.get_bot()
        iq=bot.Iq()
        iq['to']=alias
        iq['from']=bot.boundjid.full
        iq['type']='set'
        iq.append(packet)
        iq.send(False, now=True)

    def send_disconnect(self, key, iq_id, alias):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("disconnect"))
        packet.attrib['xmlns']="hexchat:disconnect"
        logging.debug("%s:%d" % local_address + " sending disconnect request to %s:%d" % remote_address)

        id_stanza=ElementTree.Element('id')
        id_stanza.text=str(iq_id)
        packet.append(id_stanza)
        
        bot=self.get_bot()
        iq=bot.Iq()
        iq['to']=alias
        iq['from']=bot.boundjid.full
        iq['type']='set'
        iq.append(packet)
        iq.send(False, now=True)
        
    def send_connect_ack(self, key, response, from_jid):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("connect_ack"))
        packet.attrib['xmlns']="hexchat:connect_ack"
        response_stanza=ElementTree.Element("response")
        response_stanza.text=response
        packet.append(response_stanza)
        logging.debug("%s:%d" % local_address + " sending result signal to %s:%d" % remote_address)
        
        bot=[bot for bot in self.bots if bot.boundjid.bare==from_jid.bare][0]
        iq=bot.Iq()
        iq['to']=set(key[1]).pop()
        iq['from']=bot.boundjid.full
        iq['type']='result'
        iq.append(packet)
        iq.send(False, now=True)
        
    def send_connect_iq(self, key):
        bot=self.get_bot()
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("connect"))
        packet.attrib['xmlns']="hexchat:connect"
        logging.debug("%s:%d" % local_address + " sending connect request to %s:%d" % remote_address)
        bot=self.get_bot()
        iq=bot.Iq()
        iq['to']=key[1]
        iq['from']=bot.boundjid.full
        iq['type']='set'
        iq.append(packet)
        iq.send(False, now=True)
        
    def send_connect_message(self, key):
        bot=self.get_bot()
    
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("connect"))
        packet.attrib['xmlns']="hexchat:connect"
        
        logging.debug("%s:%d" % local_address + " sending connect request to %s:%d" % remote_address)
        message=bot.Message()
        message['to']=key[1]
        message['id']='1'
        message['from']=bot.boundjid.full
        message['type']='chat'
        message.append(packet)
        message.send(now=True)

    ### Methods for connection/socket creation.

    def initiate_connection(self, key, jid):
        """Initiate connection to 'local_address' and add the socket to the client sockets map."""
        (local_address, peer, remote_address)=key

        if self.whitelist!=None and not local_address in self.whitelist:
            logging.warn("client sent request to connect to %s:%d" % local_address)
            self.send_connect_ack(key, "failure", jid)
            return()
                
        try: # connect to the ip:port
            logging.debug("trying to connect to %s:%d" % local_address)
            connected_socket=socket.create_connection(local_address, timeout=2.0)
        except (socket.error, OverflowError, ValueError):
            logging.warning("could not connect to %s:%d" % local_address)
            #if it could not connect, tell the bot on the the other it could not connect
            self.send_connect_ack(key, "failure", jid)
            return()
            
        logging.debug("connecting %s:%d" % remote_address + " to %s:%d" % local_address)
        self.create_client_socket(key, connected_socket)
        self.send_connect_ack(key, "success", jid)

    def create_client_socket(self, key, socket): 
        with self.client_sockets_lock:
            self.client_sockets[key] = client_socket(self, key, socket)
            self.client_sockets[key].run()

    def create_server_socket(self, local_address, peer, remote_address):
        """Create a listener and put it in the server_sockets dictionary."""
        self.server_sockets[local_address]=server_socket(self, local_address, peer, remote_address)
        self.server_sockets[local_address].run_thread.start()
        
    def delete_socket(self, key):                
        with self.client_sockets_lock:
            del(self.client_sockets[key])
            logging.debug("%s:%d" % key[0] + " disconnected from %s:%d." % key[2])

if __name__ == '__main__':
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_disconnect)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_packet)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_connect)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Message, hexchat_connect)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_connect_ack)

    if sys.argv[1]=="-c":
        if sys.argv[2]=='-d':
            logging.basicConfig(filename=sys.argv[3],level=logging.DEBUG)
            index=4
        else:
            logging.basicConfig(filename=sys.argv[2],level=logging.WARN)
            index=3
            
        username_passwords=[]
        while index<len(sys.argv) and not sys.argv[index] in ('-w','-s'):
            username_passwords.append((sys.argv[index], sys.argv[index+1]))
            index+=2

        if sys.argv[index]=='-w':
            index+=1
            whitelist=[]
            while index<len(sys.argv) and sys.argv[index]!='-s':
                whitelist.append((sys.argv[index], int(sys.argv[index+1])))
                index+=2
        else:
            whitelist=None        

        master0=master(username_passwords, whitelist)
        if sys.argv[index]=='-s':
            index+=1
            while index<len(sys.argv):
                master0.create_server_socket((sys.argv[index],int(sys.argv[index+1])), sys.argv[index+2], (sys.argv[index+3],int(sys.argv[index+4])))
                index+=5
    else:
        #todo
        pass

    while True:
        time.sleep(1)
