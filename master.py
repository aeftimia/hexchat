import base64
import logging
import time
import threading
import socket

import sleekxmpp
import xml.etree.cElementTree as ElementTree
from client_socket import client_socket, MAX_ID
from server_socket import server_socket
from bot import bot

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
    
    return key

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

        #number of times it should login to each account
        self.num_logins=num_logins

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
            time.sleep(0.5)

        self.aliases=frozenset(map(lambda bot: bot.boundjid.full, self.bots)) 

        for index in range(len(self.bots)):
            self.bots[index].register_hexchat_handlers()

    def get_bot(self):
        with self.bot_lock:
            bot=self.bots[self.bot_index%len(self.bots)]
            self.bot_index=(self.bot_index+1)%len(self.bots)
            return bot
            
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
        
        return xml
            
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

        for key in frozenset(self.client_sockets):
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
            return

        if key in self.client_sockets:
            logging.warn("connection request received from a connected socket")   
            return

        self.initiate_connection(key, msg['to'])         

    def disconnect_handler(self, iq):
        """Handles incoming xmpp iqs for disconnections"""
        try:
            key=iq_to_key(iq['disconnect'])
        except ValueError:
            logging.warn('received bad port')
            return

        if not key in self.client_sockets:
            logging.warn("%s:%d" % key[2] + " seemed to forge a disconnect to %s:%d." % key[0])
            return
            
        #client wants to disconnect                    
        try:
            iq_id=int(iq['disconnect']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.client_sockets[key]._handle_close()
            return

        self.client_sockets[key].buffer_message(iq_id, "disconnect")
            
                    
    def data_handler(self, iq):
        """Handles incoming xmpp iqs for data"""
        try:
            key=iq_to_key(iq['packet'])
        except ValueError:
            logging.warn('received bad port')
            return
            
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
            return

        try:
            iq_id=int(iq['packet']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.client_sockets[key]._handle_close()
            return

        try:
            #extract data, ignoring bytes we already received
            data=base64.b64decode(iq['packet']['data'].encode("UTF-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            logging.warn("%s:%d received invalid data from " % key[0] + "%s:%d. Silently disconnecting." % key[2])
            #bad data can only mean trouble
            #silently disconnect
            self.client_sockets[key]._handle_close()
            return

        self.client_sockets[key].buffer_message(iq_id, data)
           
    def connect_ack_handler(self, iq):
        try:
            key=iq_to_key(iq['connect_ack'])
        except ValueError:
            logging.warn('received bad port')
            return

        key0=(key[0], iq['from'].bare, key[2])
        if not key0 in self.pending_connections:
            logging.debug("key not found in sockets or pending connections")
            return
                
        if not key0 in self.pending_connections:
            logging.warn('iq not in pending connections')
            return
           
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

    def send_data(self, key, data, iq_id, alias, bot):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element('packet'))
        packet.attrib['xmlns']="hexchat:packet"

        id_stanza=ElementTree.Element('id')
        id_stanza.text=str(iq_id)
        packet.append(id_stanza)
        
        data_stanza=ElementTree.Element('data')
        data_stanza.text=data
        packet.append(data_stanza)
        
        iq=bot.Iq()
        iq['to']=alias
        iq['from']=bot.boundjid.full
        iq['type']='set'
        iq.append(packet)
        with bot._send_lock:
            iq.send(False, now=True)

    def send_disconnect(self, key, iq_id, alias, bot):
        (local_address, remote_address)=(key[0], key[2])
        packet=self.format_header(local_address, remote_address, ElementTree.Element("disconnect"))
        packet.attrib['xmlns']="hexchat:disconnect"
        logging.debug("%s:%d" % local_address + " sending disconnect request to %s:%d" % remote_address)

        id_stanza=ElementTree.Element('id')
        id_stanza.text=str(iq_id)
        packet.append(id_stanza)
        
        iq=bot.Iq()
        iq['to']=alias
        iq['from']=bot.boundjid.full
        iq['type']='set'
        iq.append(packet)
        with bot._send_lock:
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
        with bot._send_lock:
            iq.send(False, now=True)
        
    def send_connect_iq(self, key):
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
        with bot._send_lock:
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
        with bot._send_lock:
            message.send(now=True)

    ### Methods for connection/socket creation.

    def initiate_connection(self, key, jid):
        """Initiate connection to 'local_address' and add the socket to the client sockets map."""
        (local_address, peer, remote_address)=key

        if self.whitelist!=None and not local_address in self.whitelist:
            logging.warn("client sent request to connect to %s:%d" % local_address)
            self.send_connect_ack(key, "failure", jid)
            return
                
        try: # connect to the ip:port
            logging.debug("trying to connect to %s:%d" % local_address)
            connected_socket=socket.create_connection(local_address, timeout=2.0)
        except (socket.error, OverflowError, ValueError):
            logging.warning("could not connect to %s:%d" % local_address)
            #if it could not connect, tell the bot on the the other it could not connect
            self.send_connect_ack(key, "failure", jid)
            return
            
        logging.debug("connecting %s:%d" % remote_address + " to %s:%d" % local_address)
        bot=self.send_connect_ack(key, "success", jid)
        self.create_client_socket(key, connected_socket)

    def create_client_socket(self, key, socket): 
        with self.client_sockets_lock:
            self.client_sockets[key] = client_socket(self, key, socket)
            self.client_sockets[key].run()

    def create_server_socket(self, local_address, peer, remote_address):
        """Create a listener and put it in the server_sockets dictionary."""
        self.server_sockets[local_address]=server_socket(self, local_address, peer, remote_address)
        self.server_sockets[local_address].run_thread.start()
        
    def delete_socket(self, key):
        if not key in self.client_sockets:
            return            
        with self.client_sockets_lock:
            del(self.client_sockets[key])
            logging.debug("%s:%d" % key[0] + " disconnected from %s:%d." % key[2])
