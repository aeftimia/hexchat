import base64
import logging
import time
import threading
import socket
import select

from client_socket import client_socket
from server_socket import server_socket
from bot import bot
from util import Peer_Resource_DB
from util import msg_to_key, alias_decode, karma_better, send_thread, Iq, tostring, ElementTree, format_header
from util import CONNECT_TIMEOUT, PENDING_DISCONNECT_TIMEOUT, CHECK_RATE
from util import ALLOCATED_BANDWIDTH, THROUGHPUT
from util import SELECT_TIMEOUT, SELECT_LOOP_RATE


"""this class exchanges data between tcp sockets and xmpp servers."""
class master():
    def __init__(self, jid_passwords, whitelist, num_logins, sequential_bootup):
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

        #disconnects that go out when a resource is unavailable
        #maps key => set of recipient aliases to use for sending disconnects
        self.pending_disconnects={}

        #peer's resources
        self.peer_resources=Peer_Resource_DB()

        #for multiple logins
        self.connection_requests={}

        #number of times to login to each account
        self.num_logins=num_logins

        #maps sockets to keys of client_sockets
        self.socket_map={}

        #locks
        self.client_sockets_lock=threading.Lock()
        self.pending_connections_lock=threading.Lock()
        self.peer_resources_lock=threading.Lock()
        self.connection_requests_lock=threading.Lock()
        self.pending_disconnects_lock=threading.Lock()
               
        #initialize the other sleekxmpp clients.
        self.bots=[]
        for login_num in range(self.num_logins):
            for jid_password in jid_passwords:
                bot0=bot(self, jid_password)
                if sequential_bootup:
                    bot0.boot()
                else:
                    threading.Thread(name="booting %d %d" % (hash(jid_password), login_num), target=lambda: bot0.boot()).start()
                self.bots.append(bot0)
                    
        while False in map(lambda bot: bot.session_started_event.is_set(), self.bots):
            time.sleep(CHECK_RATE)

        for index in range(len(self.bots)):
            self.bots[index].register_hexchat_handlers()

        threading.Thread(name="loop %d" % hash(frozenset(map(lambda bot: bot.boundjid.full, self.bots))), target=lambda: self.select_loop()).start()

    def select_loop(self):
        while True:
            time.sleep(SELECT_LOOP_RATE)
            with self.client_sockets_lock:

                if not self.socket_map:
                    time.sleep(SELECT_TIMEOUT)
                    continue
                sockets=list(self.socket_map)
                (readable, writable, error)=select.select(sockets, sockets, sockets, SELECT_TIMEOUT)

                #error
                for socket in error:
                    key=self.socket_map[socket]
                    client_socket=self.client_sockets[key]
                    client_socket.handle_expt_event()               

                #write
                for socket in writable:
                    if not socket in self.socket_map:
                        continue
                    key=self.socket_map[socket]
                    client_socket=self.client_sockets[key]
                    write_buffer=client_socket.write_buffer
                    if write_buffer:
                        bytes=client_socket.send(write_buffer)
                        client_socket.write_buffer=write_buffer[bytes:]
                        
                #read
                for socket in readable:
                    if not socket in self.socket_map:
                        continue
                    key=self.socket_map[socket]
                    client_socket=self.client_sockets[key]
                    if not client_socket.reading:
                         continue
                    client_socket.recv()
            
    def get_best_karma(self, indices):
        selected_index=None
        while selected_index==None:
            for index in indices:
                bot=self.bots[index]
                if bot.session_started_event.is_set():
                    selected_index=index
                    selected_karma=bot.get_karma()
                    break
                    
        now=time.time()            
        for index in indices:
            if index is selected_index or not self.bots[index].session_started_event.is_set(): #bot might have been disconnected and is waiting to reconnect
                continue
                
            karma=self.bots[index].get_karma()
            if karma_better(karma, selected_karma, now):
                self.bots[selected_index].karma_lock.release()
                selected_index=index
                selected_karma=karma
            else:
                self.bots[index].karma_lock.release()

        return selected_index #with karma_lock still acquired

    def get_aliases(self):            
        index_list=[]
        accumulated_bandwidth=0
        client_index_list=[]
        
        while not client_index_list:
            for bot_index, bot in enumerate(self.bots):
                if bot.session_started_event.is_set(): 
                    client_index_list.append((bot_index, bot.get_num_clients()))
            
        client_index_list.sort(key=lambda element: element[1])
        for element in client_index_list:
            (bot_index, num_clients)=element
            if self.bots[bot_index].session_started_event.is_set():
                index_list.append(bot_index)
                accumulated_bandwidth+=THROUGHPUT/(num_clients+1)
                if accumulated_bandwidth>=ALLOCATED_BANDWIDTH:
                    break
                          
        for index in index_list:
            self.bots[index].num_clients+=1
            
        while client_index_list:
            self.bots[client_index_list.pop()[0]].num_clients_lock.release()
            
        return index_list

    def add_aliases(self, xml, aliases):
        aliases_stanza=ElementTree.Element("aliases")
        bots=self.aliases_to_bots(aliases)
        servers={}
        for bot in bots:
            while not bot.session_started_event.is_set():
                time.sleep(CHECK_RATE)
            jid=bot.boundjid
            user=jid.user
            server=jid.server
            resource=jid.resource
            if server in servers:
                if user in servers[server]:
                    servers[server][user].add(resource)
                else:
                    servers[server][user]=set([resource])
            else:
                servers[server]={user : set([resource])}

        for server in servers:
            server_stanza=ElementTree.Element(server)
            for user in servers[server]:
                user_stanza=ElementTree.Element(user)
                user_stanza.text=",".join(servers[server][user])
                server_stanza.append(user_stanza)
            aliases_stanza.append(server_stanza)
            
        xml.append(aliases_stanza)

        return xml

    def aliases_to_bots(self, from_aliases):
       return map(lambda index: self.bots[index], from_aliases)

    def iq_to_key(self, iq, jid):
        if len(iq['remote_port'])>6 or len(iq['local_port'])>6:
            #these ports are way too long
            raise(ValueError)
        
        local_port=int(iq['remote_port'])
        remote_port=int(iq['local_port'])
            
        local_ip=iq['remote_ip']
        remote_ip=iq['local_ip']

        local_address=(local_ip, local_port)
        remote_address=(remote_ip,remote_port)

        self.client_sockets_lock.acquire()
        
        for key in self.client_sockets:
            if jid in key[1] and local_address==key[0] and remote_address==key[2]:
                return key

        self.client_sockets_lock.release()
        raise KeyError
            
    #incomming xml handlers

    def error_handler(self, iq):
        logging.warn(iq['from'].full + " unavailable")
        with self.peer_resources_lock:
            if iq['from'].bare in self.peer_resources:
                if iq['from'].full in self.peer_resources[iq['from'].bare]:
                    self.peer_resources[iq['from'].bare].remove(iq['from'].bare, iq['from'].full)

        with self.pending_connections_lock:
            for key0 in self.pending_connections:
                if iq['from'].bare==key0[1]:
                    sock=self.pending_connections[key0][1]
                    sock.close()
                    del(self.pending_connections[key0])

        with self.pending_disconnects_lock:
            for key in self.pending_disconnects.copy():
                if iq['from'].full in self.pending_disconnects[key][1]:
                    self.pending_disconnects[key][1].remove(iq['from'].full)
                    if self.pending_disconnects[key][1]:
                        self.send_disconnect(key, self.pending_disconnects[key][0], self.pending_disconnects[key][1].copy().pop())
                        self.pending_disconnect_timeout(key, self.pending_disconnects[key][1])
                    else:
                        del(self.pending_disconnects[key])

        with self.client_sockets_lock:
            for key in self.client_sockets:
                if iq['from'].full in key:
                    from_aliases=self.client_socekets.get_from_aliases()
                    self.close_socket(key)
                    to_aliases=set(key[1]).remove([iq['from'].full])
                    if to_aliases:
                        with self.pending_disconnects_lock:
                            self.pending_disconnects[key]=(from_aliases, to_aliases)
                            self.send_disconnect(key, from_aliases, to_aliases.copy().pop())
                            self.pending_disconnect_timeout(key, to_aliases)

    def connect_handler(self, msg):
        aliases=alias_decode(msg, 'connect')
        if not msg['from'].full in aliases:
            logging.warn("received message with a from address that is not in its aliases")
            return
                    
        try:
            key=msg_to_key(msg['connect'], aliases)
        except ValueError:
            logging.warn('received invalid connect')
            return
        
        threading.Thread(name="initate connection %d" % hash(key), target=lambda: self.initiate_connection(key, msg['to'])).start() 

    def connect_ack_handler(self, iq):
        try:
            key0=msg_to_key(iq['connect_ack'], iq['from'].bare)
        except ValueError:
            logging.warn('received invalid connect_ack')
            return
            
        logging.debug("%s:%d received connection result: " % key0[0] + iq['connect_ack']['response'] + " from %s:%d" % key0[2])
        if iq['connect_ack']['response']=="failure":
            with self.pending_connections_lock:
                self.pending_connections[key0][1].close()
                del(self.pending_connections[key0])
                return
    
        aliases=alias_decode(iq, 'connect_ack')
        
        with self.pending_connections_lock:
            if not key0 in self.pending_connections:
                logging.warn('iq not in pending connections')
                return
                
            with self.peer_resources_lock:
                self.peer_resources.add(key0[1], iq['from'].full)
            
            (from_aliases, socket)=self.pending_connections.pop(key0)
            key=(key0[0], aliases, key0[2])
                
        with self.client_sockets_lock:
            self.create_client_socket(key, from_aliases, socket)

    def disconnect_handler(self, iq):
        """Handles incoming xmpp iqs for disconnections"""
        try:
            key=self.iq_to_key(iq['disconnect'], iq['from'].full)
        except ValueError:
            logging.warn('received bad port')
            return
        except KeyError:
            iq=iq['disconnect']
            logging.warn("%s:%s seemed to forge a disconnect to %s:%s." % (iq['local_ip'],iq['local_port'],iq['remote_ip'],iq['remote_port']))
            return
            
        #client wants to disconnect
        if iq['disconnect']['id']=="None":
            self.close_socket(key)
            self.client_sockets_lock.release()
            return

        try:
            iq_id=int(iq['disconnect']['id'])
        except ValueError:
            logging.warn("received bad id. Disconnecting")
            self.close_socket(key)
            self.client_sockets_lock.release()
            return

        self.client_sockets[key].buffer_message(iq_id, "disconnect")

    def disconnect_error_handler(self, msg):
        """Handles incoming xmpp messages for disconnections due to errors"""
        
        aliases=alias_decode(msg, 'disconnect_error')
        if not msg['from'].full in aliases:
            logging.warn("received message with a from address that is not in its aliases")
            return
                    
        try:
            key=msg_to_key(msg['disconnect_error'], aliases)
        except ValueError:
            logging.warn('received bad port')
            return
            
        with self.client_sockets_lock:
            if key in self.client_sockets:
                self.close_socket(key)
            else:
                msg=msg['disconnect_error']
                logging.warn("%s:%s seemed to forge a disconnect to %s:%s." % (msg['local_ip'],msg['local_port'],msg['remote_ip'],msg['remote_port']))

    def data_handler(self, iq):
        """Handles incoming xmpp iqs for data"""
        try:
            key=self.iq_to_key(iq['packet'], iq['from'].full)
        except ValueError:
            logging.warn('received bad port')
            return
        except KeyError:
            iq=iq['packet']
            logging.warn("%s:%s received %d bytes from %s:%s, but is not connected." % (iq['remote_ip'],iq['remote_port'],len(iq['data']),iq['local_ip'],iq['local_port']))
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
            logging.warn("%s:%d received invalid data from " % key[0] + "%s:%d. Silently disconnecting." % key[2])
            #bad data can only mean trouble
            #silently disconnect
            self.close_socket(key)
            self.client_sockets_lock.release()
            return

        self.client_sockets[key].buffer_message(iq_id, data)

    #methods for sending xml
        
    def send_connect_ack(self, key, response, jid):
        (local_address, remote_address)=(key[0], key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element("connect_ack"))
        packet.attrib['xmlns']="hexchat:connect_ack"
        response_stanza=ElementTree.Element("response")
        response_stanza.text=response
        packet.append(response_stanza)

        if response=="success":
            aliases=self.get_aliases()
            packet=self.add_aliases(packet, aliases)
            
        logging.debug("%s:%d" % local_address + " sending result signal to %s:%d" % remote_address)
        
        indices=[index for index, bot in enumerate(self.bots) if bot.boundjid.bare==jid.bare]
        iq=Iq()
        iq['to']=set(key[1]).pop()
        iq['type']='result'
        iq.append(packet)

        self.send(iq, indices, now=True)
        
        if response=="success":
            return aliases

    def send_disconnect(self, key, from_aliases, to_alias, iq_id="None"):
        (local_address, remote_address)=(key[0], key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element("disconnect"))
        packet.attrib['xmlns']="hexchat:disconnect"
        logging.debug("%s:%d" % local_address + " sending disconnect request to %s:%d" % remote_address)

        iq=Iq()
        id_stanza=ElementTree.Element('id')
        id_stanza.text=str(iq_id)
        packet.append(id_stanza)
            
        iq['to']=to_alias
        iq['type']='set'
        iq.append(packet)
        
        self.send(iq, from_aliases, now=True)

    def send(self, data, aliases, now=False):
        selected_index=self.get_best_karma(aliases)
        selected_bot=self.bots[selected_index]
        data['from']=selected_bot.boundjid.full
        str_data = tostring(data.xml, top_level=True)
        num_bytes=len(str_data)
        selected_bot.set_karma(num_bytes)
        if now:
            threading.Thread(name="send from %s to %s" % (data['from'], data['to']), target=lambda: send_thread(str_data, selected_bot)).start()
        else:   
            selected_bot.send_queue.put(str_data)

        return len(str_data)

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
                
        (local_address, peer, remote_address)=key

        if self.whitelist!=None and not local_address in self.whitelist:
            logging.warn("client sent request to connect to %s:%d" % local_address)
            self.send_connect_ack(key, "failure", jid)
            return
                
        try: # connect to the ip:port
            logging.debug("trying to connect to %s:%d" % local_address)
            connected_socket=socket.create_connection(local_address, timeout=CONNECT_TIMEOUT)
        except (socket.error, OverflowError, ValueError):
            logging.warning("could not connect to %s:%d" % local_address)
            #if it could not connect, tell the bot on the the other it could not connect
            self.send_connect_ack(key, "failure", jid)
            return
            
        with self.client_sockets_lock:
            if key in self.client_sockets:
                connected_socket.close()
                self.send_connect_ack(key, "failure", jid)
                return    
            logging.debug("connecting %s:%d" % remote_address + " to %s:%d" % local_address)
            from_aliases=self.send_connect_ack(key, "success", jid)
            self.create_client_socket(key, from_aliases, connected_socket)

    def create_client_socket(self, key, from_aliases, socket):
        self.client_sockets[key] = client_socket(self, key, from_aliases, socket)
        self.socket_map[self.client_sockets[key].socket]=key

    def create_server_socket(self, local_address, peer, remote_address):
        """Create a listener and put it in the server_sockets dictionary."""
        self.server_sockets[local_address]=server_socket(self, local_address, peer, remote_address)
        self.server_sockets[local_address].run_thread.start()

    def close_socket(self, key):
        self.client_sockets[key].handle_close()
        
    def delete_socket(self, key):
        del(self.socket_map[self.client_sockets[key].socket])
        del(self.client_sockets[key])
        logging.debug("%s:%d" % key[0] + " disconnected from %s:%d." % key[2])

    #handling pending disconnects
    def pending_disconnect_timeout(self, key, to_aliases):
        threading.Thread(name="pending disconnect timeout %d %d" % (hash(key), hash(frozenset(to_aliases))), target=lambda: self.pending_disconnect_timeout_thread(key, to_aliases)).start()
    def pending_disconnect_timeout_thread(self, key, to_aliases):
        then=time.time()+PENDING_DISCONNECT_TIMEOUT
        while time.time()<then:
            with self.pending_disconnects_lock:
                if not (key in self.pending_disconnects and self.pending_disconnects[key][1]==to_aliases):
                    return
            time.sleep(CHECK_RATE)
        
        with self.pending_disconnects_lock:
            if key in self.pending_disconnects and self.pending_disconnects[key][1]==to_aliases:
                del(self.pending_disconnects[key])
