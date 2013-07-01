import asyncore
import base64
import hashlib
import logging
import socket
import threading
<<<<<<< HEAD
import time

TIMEOUT=10.0 #seconds before closing a socket if it has not gotten a connect_ack
=======
>>>>>>> parent of 04ac4aa... listening sockets now timeout at 2.0 seconds

class server_socket(asyncore.dispatcher):
    def __init__(self, master, local_address, peer, remote_address):
        self.master=master
        self.peer=peer
        self.remote_address=remote_address
        self.socket=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setblocking(1)
        self.set_reuse_addr()
        self.bind(local_address)
        self.listen(1023)
        self.run_thread=threading.Thread(name="accept %d" % hash(local_address), target=lambda: self.accept_thread())

    def accept_thread(self):
        while True:
            connection, local_address = self.accept()
            connection_id=base64.b64encode(hashlib.sha512(str(hash(local_address)).encode("UTF-8")).digest()).decode("UTF-8")
            with self.master.pending_connections_lock:
                logging.debug("sending connection request from %s:%d" % local_address + " to %s:%d" % self.remote_address)
<<<<<<< HEAD
                key=(connection_id, self.peer, self.remote_address)
                self.master.pending_connections[key]=connection
=======
                self.master.pending_connections[(local_address, self.peer, self.remote_address)]=connection
>>>>>>> parent of 04ac4aa... listening sockets now timeout at 2.0 seconds
                if self.peer in self.master.peer_resources:
                    logging.debug("found resource, sending connection request via iq")
                    self.master.send_connect_iq((connection_id, self.master.peer_resources[self.peer], self.remote_address))
                else:
                    logging.debug("sending connection request via message")
<<<<<<< HEAD
                    self.master.send_connect_message((connection_id, self.peer, self.remote_address)) 

                threading.Thread(name="%d timeout"%hash(key), target=lambda: self.socket_timeout(key)).start()   
=======
                    self.master.send_connect_message((local_address, self.peer, self.remote_address))       
>>>>>>> parent of 04ac4aa... listening sockets now timeout at 2.0 seconds

