import logging
import sleekxmpp
import sleekxmpp.xmlstream.handler.callback as callback
import sleekxmpp.xmlstream.matcher.stanzapath as stanzapath
from stanza_plugins import *

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

