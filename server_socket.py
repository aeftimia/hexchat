import asyncore
import logging
import socket
import threading
import time

from util import format_header, ElementTree, Iq, Message
from util import TIMEOUT, CHECK_RATE

class server_socket(asyncore.dispatcher):
    def __init__(self, master, local_address, peer, remote_address):
        self.master=master
        self.local_address=local_address
        self.peer=peer
        self.remote_address=remote_address
        self.socket=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setblocking(1)
        self.set_reuse_addr()
        self.bind(local_address)
        self.listen(8192)
        self.run_thread=threading.Thread(name="accept %d" % hash(local_address), target=lambda: self.accept_thread())

    def accept_thread(self):
        while True:
            connection, local_address = self.accept()
            aliases=self.master.get_aliases()
            with self.master.pending_connections_lock:
                logging.debug("sending connection request from %s:%d" % local_address + " to %s:%d" % self.remote_address)
                key=(local_address, self.peer, self.remote_address)
                self.master.pending_connections[key]=(aliases, connection)
                with self.master.peer_resources_lock:
                    if self.peer in self.master.peer_resources:
                        logging.debug("found resource, sending connection request via iq")
                        self.send_connect((local_address, self.master.peer_resources[self.peer], self.remote_address), aliases)
                    else:
                        logging.debug("sending connection request via message")
                        self.send_connect((local_address, self.peer, self.remote_address), aliases, message=True) 

                threading.Thread(name="%d timeout"%hash(key), target=lambda: self.socket_timeout(key, aliases)).start()   

    def socket_timeout(self, key, aliases):
        then=time.time()+TIMEOUT
        while time.time()<then:
            with self.master.pending_connections_lock:
                if not key in self.master.pending_connections:
                    return
            time.sleep(CHECK_RATE)
            
        with self.master.pending_connections_lock:
            if not key in self.master.pending_connections:
                return
            (from_aliases, socket)=self.master.pending_connections.pop(key)
            
        socket.close()
        
        for bot_index in from_aliases:
            with self.master.bots[bot_index].num_clients_lock:
                self.master.bots[bot_index].num_clients-=1
                
        with self.master.peer_resources_lock:
            if key[1] in self.master.peer_resources:
                self.send_disconnect_error(key, from_aliases, self.master.peer_resources[key[1]])
            else:
                self.send_disconnect_error(key, from_aliases, key[1], message=True)

    #methods for sending xml
        
    def send_connect(self, key, aliases, message=False):
        (local_address, remote_address)=(key[0], key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element("connect"))
        packet.attrib['xmlns']="hexchat:connect"

        packet=self.master.add_aliases(packet, aliases)
        
        logging.debug("%s:%d" % local_address + " sending connect request to %s:%d" % remote_address)

        if message:
            msg=Message()
            msg['type']='chat'
        else:  
            msg=Iq()
            msg['type']='set'
            
        msg['to']=key[1]
        msg.append(packet)
        
        self.master.send(msg, aliases, now=True)

    def send_disconnect_error(self, key, from_aliases, to_alias, message=False):
        (local_address, remote_address)=(key[0], key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element("disconnect_error"))
        packet.attrib['xmlns']="hexchat:disconnect_error"
        packet=self.master.add_aliases(packet, from_aliases)
        logging.debug("%s:%d" % local_address + " sending disconnect_error request to %s:%d" % remote_address)

        if message:
            msg=Message()
            msg['type']='chat'
        else:
            msg=Iq()
            msg['type']='set'
            
        msg['to']=to_alias
        msg.append(packet)

        self.master.send(msg, from_aliases, now=True)
                    
