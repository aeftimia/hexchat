import asyncore
import logging
import socket
import threading
import time

TIMEOUT=300.0 #seconds before closing a socket if it has not gotten a connect_ack
CHECK_TIME=0.5

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
                        self.master.send_connect_iq((local_address, self.master.peer_resources[self.peer], self.remote_address), aliases)
                    else:
                        logging.debug("sending connection request via message")
                        self.master.send_connect_message((local_address, self.peer, self.remote_address), aliases) 

                threading.Thread(name="%d timeout"%hash(key), target=lambda: self.socket_timeout(key, aliases)).start()   

    def socket_timeout(self, key, aliases):
        then=time.time()+TIMEOUT
        while time.time()<then:
            with self.master.pending_connections_lock:
                if not key in self.master.pending_connections:
                    return
            time.sleep(CHECK_TIME)
            
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
                self.master.send_disconnect_error(key, from_aliases, self.master.peer_resources[key[1]])
            else:
                self.master.send_disconnect_error(key, from_aliases, key[1], message=True)
                    
