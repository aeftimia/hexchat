import asyncore
import logging
import socket
import threading

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

