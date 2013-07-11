import logging
import time
import threading
import sleekxmpp
import sleekxmpp.xmlstream.handler.callback as callback
import sleekxmpp.xmlstream.matcher.stanzapath as stanzapath
from stanza_plugins import *

from sleekxmpp.util import QueueEmpty
import socket as Socket
import ssl

'''
Karma is defined as the average number of bytes sent over a window of KARMA_RESET
'''

from util import KARMA_RESET, THROUGHPUT

class bot(sleekxmpp.ClientXMPP):
    '''
    Bot used to send and receive messages
    '''
    def __init__(self, master, jid_password):
        self.master=master
        self.karma=0.0
        self.time_last_sent=time.time() #times at which the bot last sent a message
        self.karma_lock=threading.Lock()
        self.num_clients=0 #number of clients sockets using this bot
        self.num_clients_lock=threading.Lock()
        self.__failed_send_stanza=None #some sleekxmpp thing for resending stuff
        sleekxmpp.ClientXMPP.__init__(self, *jid_password)

        # gmail xmpp server is actually at talk.google.com
        if jid_password[0].find("@gmail.com")!=-1:
            self.connect_address = ("talk.google.com", 5222)
        else:
            self.connect_address = None
            
        # event handlers are sleekxmpp's way of dealing with important xml tags it receives
        self.add_event_handler("session_start", lambda event: self.session_start())
        self.add_event_handler("disconnected", lambda event: self.disconnected())

        self.register_plugin('xep_0199') # Ping

    def set_karma(self, num_bytes):
        '''
        set the karma and time_last_sent
        karma_lock should have been acquired before calling this function
        '''
        now=time.time()
        dtime=now-self.time_last_sent
        if dtime>KARMA_RESET:
            self.karma=num_bytes
        else:
            '''
            compute moving average
            note that as dtime-->KARMA_RESET, the new self.karma-->num_bytes
            and as dtime-->0, the new self.karma-->num_bytes+self.karma
            '''
            self.karma=num_bytes+self.karma*(1-dtime/KARMA_RESET)

        self.time_last_sent=now
        self.karma_lock.release()


    def get_karma(self):
        self.karma_lock.acquire()
        return (self.karma, self.time_last_sent)

    def get_num_clients(self):
        self.num_clients_lock.acquire()
        return self.num_clients

    def register_hexchat_handlers(self):
        
        '''these handle the custom iq stanzas'''
        
        self.register_handler(callback.Callback('Connect Handler',stanzapath.StanzaPath('iq@type=set/connect'),self.master.connect_handler))
        self.register_handler(callback.Callback('Connect Message Handler',stanzapath.StanzaPath('message@type=chat/connect'),self.master.connect_handler))
        self.register_handler(callback.Callback('Connect Ack Handler',stanzapath.StanzaPath('iq@type=result/connect_ack'),self.master.connect_ack_handler))
        self.register_handler(callback.Callback('Data Handler',stanzapath.StanzaPath('iq@type=set/packet'),self.master.data_handler))
        self.register_handler(callback.Callback('Disconnect Handler',stanzapath.StanzaPath('iq@type=set/disconnect'),self.master.disconnect_handler))
        self.register_handler(callback.Callback('Disconnect Error Message Handler',stanzapath.StanzaPath('message@type=chat/disconnect_error'),self.master.disconnect_error_handler))
        self.register_handler(callback.Callback('Disconnect Error Iq Handler',stanzapath.StanzaPath('iq@type=set/disconnect_error'),self.master.disconnect_error_handler))

        self.register_handler(callback.Callback('IQ Error Handler',stanzapath.StanzaPath('iq@type=error/error'), self.master.error_handler))
        self.register_handler(callback.Callback('Message Error Handler',stanzapath.StanzaPath('message@type=error/error'),self.master.error_handler))

    ### session management mathods:

    def boot(self, process=True):
        if self.connect(self.connect_address):
            if process:
                self.process()
        else:
            raise(Exception(self.boundjid.bare+" could not connect"))

    def session_start(self):
        
        """Called when the bot connects and establishes a session with the XMPP server."""
        
        # XMPP spec says that we should broadcast our presence when we connect.
        self.send_presence()
        
    def disconnected(self):
        
        '''
        Called when the bot disconnects from the XMPP server.
        Try to reconnect.
        '''
        
        logging.warning("XMPP chat server disconnected")
        logging.debug("Trying to reconnect")
        self.boot(False)
        self.send_presence()

    def _send_thread(self):
        
        '''
         modifed version of sleekxmpp's _send_thread
         that will not send faster than THROUGHPUT
        '''
        
        try:
            while not self.stop.is_set():
                while not self.stop.is_set() and \
                      not self.session_started_event.is_set():
                    self.session_started_event.wait(timeout=0.1)
                if self.__failed_send_stanza is not None:
                    data = self.__failed_send_stanza
                    self.__failed_send_stanza = None
                else:
                    try:
                        data = self.send_queue.get(True, 1)
                    except QueueEmpty:
                        continue
                logging.debug("SEND: %s", data)
                enc_data = data.encode('utf-8')
                total = len(enc_data)
                sent = 0
                count = 0
                tries = 0
                try:
                    with self.send_lock:
                        while sent < total and not self.stop.is_set() and \
                              self.session_started_event.is_set():
                            try:
                                bytes = self.socket.send(enc_data[sent:])
                                sent += bytes
                                count += 1
                                '''
                                throttling code
                                that prevents data from being sent
                                faster than THROUGHPUT
                                '''
                                time.sleep(bytes/THROUGHPUT)
                            except ssl.SSLError as serr:
                                if tries >= self.ssl_retry_max:
                                    logging.debug('SSL error: max retries reached')
                                    self.exception(serr)
                                    logging.warning("Failed to send %s", data)
                                    if not self.stop.is_set():
                                        self.disconnect(self.auto_reconnect,
                                                        send_close=False)
                                    logging.warning('SSL write error: retrying')
                                if not self.stop.is_set():
                                    time.sleep(self.ssl_retry_delay)
                                tries += 1
                    if count > 1:
                        logging.debug('SENT: %d chunks', count)
                    self.send_queue.task_done()
                except (Socket.error, ssl.SSLError) as serr:
                    self.event('socket_error', serr, direct=True)
                    logging.warning("Failed to send %s", data)
                    if not self.stop.is_set():
                        self.__failed_send_stanza = data
                        self._end_thread('send')
                        self.disconnect(self.auto_reconnect, send_close=False)
                        return
        except Exception as ex:
            logging.exception('Unexpected error in send thread: %s', ex)
            self.exception(ex)
            if not self.stop.is_set():
                self._end_thread('send')
                self.disconnect(self.auto_reconnect)
                return

        self._end_thread('send')
