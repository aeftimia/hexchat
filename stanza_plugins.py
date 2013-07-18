import sleekxmpp

class hexchat_connect(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'connect'
    namespace = 'hexchat:connect'
    plugin_attrib = 'connect'
    interfaces = set(('local_ip', 'local_port', 'remote_ip','remote_port'))
    sub_interfaces = interfaces
    bool_interfaces = interfaces

class hexchat_connect_ack(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'connect_ack'
    namespace = 'hexchat:connect_ack'
    plugin_attrib = 'connect_ack'
    interfaces = set(('local_ip','local_port', 'remote_ip', 'remote_port', 'response'))
    sub_interfaces=interfaces
    bool_interfaces = interfaces

class hexchat_packet(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'packet'
    namespace = 'hexchat:packet'
    plugin_attrib = 'packet'
    interfaces = set(('local_ip', 'local_port', 'remote_ip', 'remote_port','data', 'id'))
    sub_interfaces = interfaces
    bool_interfaces = interfaces

class hexchat_disconnect(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'disconnect'
    namespace = 'hexchat:disconnect'
    plugin_attrib = 'disconnect'
    interfaces = set(('local_ip', 'local_port', 'remote_ip', 'remote_port', 'id'))
    sub_interfaces = interfaces
    bool_interfaces = interfaces

class hexchat_disconnect_error(sleekxmpp.xmlstream.stanzabase.ElementBase):
    name = 'disconnect_error'
    namespace = 'hexchat:disconnect_error'
    plugin_attrib = 'disconnect_error'
    interfaces = set(('local_ip', 'local_port', 'remote_ip', 'remote_port'))
    sub_interfaces = interfaces
    bool_interfaces = interfaces

def register_stanza_plugins():
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Message, hexchat_connect)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_connect)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_connect_ack)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_packet)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_disconnect)

    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Message, hexchat_disconnect_error)
    sleekxmpp.xmlstream.register_stanza_plugin(sleekxmpp.stanza.Iq, hexchat_disconnect_error)
