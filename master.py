import base64
import logging
import time
import threading
import socket
from operator import itemgetter

import sleekxmpp
import xml.etree.cElementTree as ElementTree
from sleekxmpp.xmlstream import tostring
from sleekxmpp.stanza import Message, Iq

from client_socket import client_socket, MAX_ID
from server_socket import server_socket
from bot import bot

CONNECT_TIMEOUT=1.0

            
def check_aliases_and_port(aliases, remote_port, mfrom):
    aliases=frozenset(aliases.split(','))
    if not mfrom in aliases:
        logging.warn('received message from a JID that did not include itself in its aliases')
        return False
            
    try:
        if len(remote_port)>6:
            raise ValueError
        remote_port=int(remote_port)
    except ValueError:
        logging.warn('received bad port')
        return False

    return (aliases, remote_port)

"""this class exchanges data between tcp sockets and xmpp servers."""
class master():
    def __init__(self, jid_passwords, whitelist, num_logins):
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

        #for multiple logins
        self.connection_requests={}

        #number of times to login to each account
        self.num_logins=num_logins

        #locks
        self.client_sockets_lock=threading.Lock()
        self.pending_connections_lock=threading.Lock()
        self.peer_resources_lock=threading.Lock()
        self.connection_requests_lock=threading.Lock()
               
        #initialize the other sleekxmpp clients.
        self.bots=[]
        for _ in range(self.num_logins):
            for jid_password in jid_passwords:
                self.bots.append(bot(self, jid_password))

        self.bot_index=0

        while False in map(lambda bot: bot.session_started_event.is_set(), self.bots):
            time.sleep(1.0)

        self.aliases=",".join(map(lambda bot: bot.boundjid.full, self.bots)) 

        for index in range(len(self.bots)):
            self.bots[index].register_hexchat_handlers()
            
    #misc methods for manipulating messages
    
    def format_header(self, connection_id, remote_address, xml):       
        connection_id_stanza=ElementTree.Element("connection_id")
        connection_id_stanza.text=connection_id
        xml.append(connection_id_stanza)
        
        remote_ip_stanza=ElementTree.Element("remote_ip")
        remote_ip_stanza.text=remote_address[0]
        xml.append(remote_ip_stanza)

        remote_port_stanza=ElementTree.Element("remote_port")
        remote_port_stanza.text=str(remote_address[1])
        xml.append(remote_port_stanza)
        
        return xml

    def add_aliases(self, xml):
        aliases_stanza=ElementTree.Element("aliases")
        aliases_stanza.text=self.aliases
        xml.append(aliases_stanza)

        return xml

    def iq_to_key(self, iq, jid):
        if len(iq['remote_port'])>6:
            #this port is way too long
            raise(ValueError)
        
        remote_port=int(iq['remote_port'])
        remote_ip=iq['remote_ip']

        remote_address=(remote_ip, remote_port)
        
        self.client_sockets_lock.acquire()
        for key in self.client_sockets:
            if jid in key[1] and iq['connection_id']==key[0] and remote_address==key[2]:
                return key

        self.client_sockets_lock.release()       
        raise KeyError

    #incomming xml handlers

    def error_handler(self, iq):
        logging.warn("%s not available."%iq['from'].full)
        
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

        self.client_sockets_lock.acquire()
        for key in self.client_sockets:
            if iq['from'].full in self.client_sockets[key].aliases:
                if len(self.client_sockets[key].aliases)>1:
                    with self.client_sockets[key].alias_lock:
                        self.client_sockets[key].aliases=list(frozenset(self.client_sockets[key].aliases)-frozenset([iq['from'].full]))
                    with self.client_sockets[key].id_lock:
                        self.client_sockets[key].id=(self.client_sockets[key].id-1)%MAX_ID
                else:
                    self.close_socket(key)
        self.client_sockets_lock.release()

    def connect_handler(self, msg):          
        aliases_and_remote_port=check_aliases_and_port(msg['connect']['aliases'], msg['connect']['remote_port'], msg['from'].full)
        if not aliases_and_remote_port:
            return
        (aliases, remote_port)=aliases_and_remote_port
        
        key=(msg['connect']['connection_id'], aliases, (msg['connect']['remote_ip'], remote_port))
        threading.Thread(name="initate connection %d" % hash(key), target=lambda: self.initiate_connection(key, msg['to'])).start() 

    def connect_ack_handler(self, iq):
        aliases_and_remote_port=check_aliases_and_port(iq['connect_ack']['aliases'], iq['connect_ack']['remote_port'], iq['from'].full)
        if not aliases_and_remote_port:
            return
        (aliases, remote_port)=aliases_and_remote_port

        key0=(iq['connect_ack']['connection_id'], iq['from'].bare, (iq['connect_ack']['remote_ip'], remote_port))
           
        with self.pending_connections_lock:
            if not key0 in self.pending_connections:
                logging.warn('iq not in pending connections')
                return
                
            with self.peer_resources_lock:
                self.peer_resources[key0[1]]=iq['from'].full
            
            logging.debug("connection %s received connection result: " % key0[0] + " %s" % iq['connect_ack']['response'] + " from %s:%d" % key0[2])
            if iq['connect_ack']['response']=="failure":
                self.pending_connections[key0].close()
                del(self.pending_connections[key0])
                return
            else:
                socket=self.pending_connections.pop(key0)
                
        with self.client_sockets_lock:
            self.create_client_socket((key0[0], aliases, key0[2]), socket)

    def disconnect_handler(self, iq):
        """Handles incoming xmpp iqs for disconnections"""
        try:
            key=self.iq_to_key(iq['disconnect'],iq['from'].full)
        except ValueError:
            logging.warn('received bad port')
            return
        except KeyError:
            iq=iq['disconnect']
            logging.warn("connection %s seemed to forge a disconnect to %s:%s." % (iq['connection_id'],iq['remote_ip'],iq['remote_port']))
            return
            
        #client wants to disconnect                    
        try:
            iq_id=int(iq['disconnect']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.close_socket(key)
            self.client_sockets_lock.release()
            return

        self.client_sockets[key].buffer_message(iq_id, "disconnect")

    def data_handler(self, iq):
        """Handles incoming xmpp iqs for data"""
        try:
            key=self.iq_to_key(iq['packet'],iq['from'].full)
        except ValueError:
            logging.warn('received bad port')
            return
        except KeyError:
            iq=iq['packet']
            logging.warn("%s:%s received %d bytes from connection %s, but is not connected." % (iq['remote_ip'],iq['remote_port'],len(iq['data'])/2,iq['connection_id']))
            return

        try:
            iq_id=int(iq['packet']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.close_socket(key)
            self.client_sockets_lock.release()
            return

        try:
            #extract data, ignoring bytes we already received
            data=base64.b64decode(iq['packet']['data'].encode("UTF-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            logging.warn("connection %s received invalid data from " % key[0] + "%s:%d. Silently disconnecting." % key[2])
            #bad data can only mean trouble
            #silently disconnect
            self.close_socket(key)
            self.client_sockets_lock.release()
            return

        self.client_sockets[key].buffer_message(iq_id, data)

    #methods for sending xml

    def send_data(self, key, data, iq_id, alias):
        (connection_id, remote_address)=(key[0], key[2])
        packet=self.format_header(connection_id, remote_address, ElementTree.Element('packet'))
        packet.attrib['xmlns']="hexchat:packet"

        id_stanza=ElementTree.Element('id')
        id_stanza.text=str(iq_id)
        packet.append(id_stanza)
        
        data_stanza=ElementTree.Element('data')
        data_stanza.text=data
        packet.append(data_stanza)

        iq=Iq()
        iq['to']=alias
        iq['type']='set'
        iq.append(packet)
        
        self.send(iq)

    def send_disconnect(self, key, iq_id, alias):
        (connection_id, remote_address)=(key[0], key[2])
        packet=self.format_header(connection_id, remote_address, ElementTree.Element("disconnect"))
        packet.attrib['xmlns']="hexchat:disconnect"
        logging.debug("connection %s" % connection_id + " sending disconnect request to %s:%d" % remote_address)

        id_stanza=ElementTree.Element('id')
        id_stanza.text=str(iq_id)
        packet.append(id_stanza)
        
        iq=Iq()
        iq['to']=alias
        iq['type']='set'
        iq.append(packet)
        
        self.send(iq)

                
    def send_connect_ack(self, key, response, jid):
        (connection_id, remote_address)=(key[0], key[2])
        packet=self.format_header(connection_id, remote_address, ElementTree.Element("connect_ack"))
        packet.attrib['xmlns']="hexchat:connect_ack"
        response_stanza=ElementTree.Element("response")
        response_stanza.text=response
        packet.append(response_stanza)

        if response=="success":
            packet=self.add_aliases(packet)
            
        logging.debug(connection_id + " sending result signal to %s:%d" % remote_address)
        
        bot=[bot for bot in self.bots if bot.boundjid.bare==jid.bare][0]
        iq=Iq()
        iq['to']=set(key[1]).pop()
        iq['type']='result'
        iq.append(packet)
        str_data=tostring(iq.xml, top_level=True)
        bot.karma_lock.acquire()
        bot.set_karma(len(str_data))
        bot.send_queue.put(str_data)
            
        
    def send_connect_iq(self, key):
        (connection_id, remote_address)=(key[0], key[2])
        packet=self.format_header(connection_id, remote_address, ElementTree.Element("connect"))
        packet.attrib['xmlns']="hexchat:connect"

        packet=self.add_aliases(packet)
        
        logging.debug(connection_id + " sending connect request to %s:%d" % remote_address)
          
        iq=Iq()
        iq['to']=key[1]
        iq['type']='set'
        iq.append(packet)
        
        self.send(iq)   
        
    def send_connect_message(self, key):
        (connection_id, remote_address)=(key[0], key[2])
        packet=self.format_header(connection_id, remote_address, ElementTree.Element("connect"))
        packet.attrib['xmlns']="hexchat:connect"

        packet=self.add_aliases(packet)
               
        logging.debug(connection_id + " sending connect request to %s:%d" % remote_address)
        
        message=Message()
        message['to']=key[1]
        message['type']='chat'
        message.append(packet)
        
        self.send(message, append_from=True)

    def send(self, data, append_from=False):
        selected_bot=self.bots[0]
        selected_bot_karma=selected_bot.get_karma()
        for bot in self.bots[1:]:
            karma=bot.get_karma()
            now=time.time()
            if karma[1]/(now-karma[0])<selected_bot_karma[1]/(now-selected_bot_karma[0]):
                    selected_bot.karma_lock.release()
                    selected_bot=bot
                    selected_bot_karma=karma
            else:
                bot.karma_lock.release()

        if append_from:
            data['from']=bot.boundjid.full

        str_data = tostring(data.xml, xmlns=bot.default_ns, stream=bot, top_level=True)
        num_bytes=len(str_data)
            
        selected_bot.set_karma(num_bytes)
        selected_bot.send_queue.put(str_data)

    ### Methods for connection/socket creation.

    def initiate_connection(self, key, jid):
        """Initiate connection to 'local_address' and add the socket to the client sockets map."""
        #make sure this function only gets evaluated once per connection
        #no matter how many times we are logged into the same accounts
        if jid.full==jid.bare:
            with self.connection_requests_lock:
                if key in self.connection_requests:
                    self.connection_requests[key]+=1
                else:
                    self.connection_requests[key]=1
                if self.connection_requests[key]==self.num_logins:
                    del(self.connection_requests[key])
                else:
                    return
                
        (connection_id, peer, remote_address)=key

        if self.whitelist!=None and not remote_address in self.whitelist:
            logging.warn("client sent request to connect to %s:%d" % remote_address)
            self.send_connect_ack(key, "failure", jid)
            return
                
        try: # connect to the ip:port
            logging.debug("trying to connect to %s:%d" % remote_address)
            connected_socket=socket.create_connection(remote_address, timeout=CONNECT_TIMEOUT)
        except (socket.error, OverflowError, ValueError):
            logging.warning("could not connect to %s:%d" % remote_address)
            #if it could not connect, tell the bot on the the other it could not connect
            self.send_connect_ack(key, "failure", jid)
            return
            
        with self.client_sockets_lock:
            if key in self.client_sockets:
                connected_socket.close()
                self.send_connect_ack(key, "failure", jid)
                return    
            logging.debug("creating connection with id:%s" % connection_id + " to %s:%d" % remote_address)
            self.send_connect_ack(key, "success", jid)
            self.create_client_socket(key, connected_socket)

    def create_client_socket(self, key, socket):
        self.client_sockets[key] = client_socket(self, key, socket)
        self.client_sockets[key].run()

    def create_server_socket(self, local_address, peer, remote_address):
        """Create a listener and put it in the server_sockets dictionary."""
        self.server_sockets[local_address]=server_socket(self, local_address, peer, remote_address)
        self.server_sockets[local_address].run_thread.start()

    def close_socket(self, key):
        threading.Thread(name="close %d"%hash(key), target=lambda: self.client_sockets[key].handle_close()).start()
        
    def delete_socket(self, key):     
        del(self.client_sockets[key])
        logging.debug("connection %s" % key[0] + " disconnected from %s:%d." % key[2])
