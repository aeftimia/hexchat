import asyncore
import base64
import logging
import socket
import threading
import time
import sys

from util import format_header, ElementTree, Iq
from util import MAX_ID, MAX_DB_SIZE, MAX_SIZE
from util import KARMA_RESET, ALLOCATED_BANDWIDTH, THROUGHPUT

class client_socket():
    def __init__(self, master, key, from_aliases, socket):
        self.master=master
        self.key=key
        self.write_buffer=b''
        self.to_aliases=list(self.key[1])
        self.to_alias_index=0
        self.to_alias_lock=threading.Lock()
        self.from_aliases=from_aliases
        self.id=0
        self.last_id_received=0
        self.incomming_message_db={}
        self.reading=True
        socket.setblocking(0)
        self.socket=socket
        self.karma=0.0
        self.karma_lock=threading.Lock()
        self.time_last_sent=time.time()

    def get_to_alias(self):
        with self.to_alias_lock:
            to_alias=self.to_aliases[self.to_alias_index]
            self.to_alias_index=(self.to_alias_index+1)%len(self.to_aliases)
            return to_alias

    def get_id(self):
        iq_id=self.id
        self.id=(self.id+1)%MAX_ID
        return iq_id

    def send_message(self, data):
        (local_address, remote_address)=(self.key[0], self.key[2])
        packet=format_header(local_address, remote_address, ElementTree.Element('packet'))
        packet.attrib['xmlns']="hexchat:packet"

        id_stanza=ElementTree.Element('id')
        id_stanza.text=str(self.get_id())
        packet.append(id_stanza)
        
        data_stanza=ElementTree.Element('data')
        data_stanza.text=base64.b64encode(data).decode("UTF-8")
        packet.append(data_stanza)

        iq=Iq()
        iq['to']=self.get_to_alias()
        iq['type']='set'
        iq.append(packet)
        
        return self.master.send(iq, self.from_aliases)
        
    def buffer_message(self, iq_id, data):
        if data=="disconnect":
            self.reading=False
                        
        raw_id_diff=iq_id-self.last_id_received
        id_diff=raw_id_diff%MAX_ID
                        
        if raw_id_diff<0 and raw_id_diff>-MAX_ID/2. or iq_id in self.incomming_message_db or sys.getsizeof(self.incomming_message_db)>=MAX_DB_SIZE:
                logging.warn("received redundant message or too many messages in buffer. Disconnecting")
                self.handle_close(True)
                return

        if data=="disconnect":
            logging.debug("%s:%d " % self.key[0] + "received disconnect from %s:%d" % self.key[2]+ " with id:%d" % iq_id)
        else:
            logging.debug("%s:%d " % self.key[0] + "received %d bytes from " % len(data)+ "%s:%d" % self.key[2]+ " with id:%d" % iq_id)
            logging.debug("%s:%d looking for id:"%self.key[0]+str(self.last_id_received))

        self.incomming_message_db[iq_id]=data
        while self.last_id_received in self.incomming_message_db:
            data=self.incomming_message_db.pop(self.last_id_received)
            if data=="disconnect":
                self.handle_close()
                self.master.client_sockets_lock.release()
                return
            self.last_id_received=(self.last_id_received+1)%MAX_ID
            logging.debug("%s:%d now looking for id:"%self.key[0]+str(self.last_id_received))
            self.write_buffer+=data
        
        self.master.client_sockets_lock.release()

    def handle_close(self, send_disconnect=False):
        """Called when the TCP client socket closes."""
        (local_address, remote_address)=(self.key[0], self.key[2])
        logging.debug("disconnecting %s:%d from " % local_address +  "%s:%d" % remote_address)
        self.close()
        
        self.master.delete_socket(self.key)
         
        for bot_index in self.from_aliases:
            with self.master.bots[bot_index].num_clients_lock:
                self.master.bots[bot_index].num_clients-=1 
                    
        if send_disconnect:
            self.master.send_disconnect(self.key, self.from_aliases, self.get_to_alias(), self.get_id())
            with self.master.pending_disconnects_lock: #wait for an error from the chat server
                to_aliases=set(self.to_aliases)
                self.master.pending_disconnects[self.key]=(self.from_aliases, to_aliases)
                self.master.pending_disconnect_timeout(self.key, to_aliases)

    #karma methods
    def set_karma(self, num_bytes):
        now=time.time()
        dtime=now-self.time_last_sent
        if dtime>KARMA_RESET:
            self.karma=num_bytes
        else: #compute moving average
              #note that as dtime-->KARMA_RESET, the new self.karma-->num_bytes
              #and as dtime-->0, the new self.karma-->num_bytes+self.karma
            self.karma=num_bytes+self.karma*(1-dtime/KARMA_RESET)

        self.time_last_sent=now        
        self.karma_lock.release()


    def get_karma(self):
        self.karma_lock.acquire()
        return (self.karma, self.time_last_sent)

    #not currently used, but may be useful at some point
    def get_allocated_bandwidth(self):
        bandwidth=0
        for index in self.from_aliases:
            bot=self.master.bots[index]
            if bot.session_started_event.is_set():
                bandwidth+=THROUGHPUT/bot.get_num_clients()
                bot.num_clients_lock.release()
        return bandwidth

    #socket methods
    def send(self, data):            
        try:
            result = self.socket.send(data)
            return result
        except socket.error as why:
            if why.args[0] in asyncore._DISCONNECTED:
                self.handle_close(True)
                return 0
            else:
                raise

    def recv(self):            
        (karma, time_last_sent)=self.get_karma()
        dtime=time.time()-time_last_sent
        if (MAX_SIZE+karma)/(dtime+KARMA_RESET)>ALLOCATED_BANDWIDTH:
            self.karma_lock.release()
            return
        try:                
            data = self.socket.recv(MAX_SIZE)
            if not data:
                # a closed connection is indicated by signaling
                # a read condition, and having recv() return 0.
                self.handle_close(True)
                self.karma_lock.release()
            else:
                num_bytes_sent=self.send_message(data)
                self.set_karma(num_bytes_sent)
        except socket.error as why:
            # winsock sometimes throws ENOTCONN
            if why.args[0] in asyncore._DISCONNECTED:
                self.handle_close(True)
                self.karma_lock.release()
            else:
                raise

    def close(self):
        try:
            self.socket.close()
        except socket.error as why:
            if why.args[0] not in (asyncore.ENOTCONN, asyncore.EBADF):
                raise
                
    def handle_expt_event(self):
        # handle_expt_event() is called if there might be an error on the
        # socket, or if there is OOB data
        # check for the error condition first
        err = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err != 0:
            # we can get here when select.select() says that there is an
            # exceptional condition on the socket
            # since there is an error, we'll go ahead and close the socket
            # like we would in a subclassed handle_read() that received no
            # data
            self.handle_close(True)
        else:
            logging.warn('something odd happened with a socket')
