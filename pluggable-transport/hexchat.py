import pyptlib.util.checkClientMode
import pyptlib.config

if pyptlib.util.checkClientMode():
    import pyptlib.client
    try:
        managed_info = pyptlib.client.init(["rot13", "rot26"])
    except pyptlib.config.EnvError, err:
        print("pyptlib could not bootstrap ('%s')." % str(err))

    if 'fast' in managed_info['transports']:
        import fast
        try:
            port = fast.run_client()
            pyptlib.client.reportSuccess('fast', 5, ('127.0.0.1', port))
        except:
            pyptlib.client.reportFailure('fast', 'Could not Launch on any port.')
            
    if 'secure' in managed_info['transports']:
            
