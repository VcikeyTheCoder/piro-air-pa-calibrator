"""Drive pa_loadcell's SampleBus with Klipper's REAL LoadCell class."""
import os, sys, importlib
# Optional test against Klipper's REAL load_cell.py (needs a Klipper checkout
# and: pip install pyserial cffi greenlet jinja2 numpy).
# Run: KLIPPER_DIR=~/klipper python3 tests/test_real_klipper.py
HERE = os.path.dirname(os.path.abspath(__file__))
KL = os.path.expanduser(os.environ.get('KLIPPER_DIR', '~/klipper'))
sys.path.insert(0, os.path.join(KL, 'klippy'))
sys.path.insert(0, os.path.join(HERE, '..', 'klipper'))
try:
    lcmod = importlib.import_module('extras.load_cell')
except Exception as e:
    print('SKIP: cannot import Klipper load_cell (%r); set KLIPPER_DIR' % e); sys.exit(0)
from pa_loadcell.feed import SampleBus

class Gcode:
    def register_mux_command(self,*a,**k): pass
    def register_command(self,*a,**k): pass
class Reactor:
    def __init__(s): s.t=100.0; s.cbs=[]
    def monotonic(s): return s.t
    def register_callback(s,cb,*a): s.cbs.append(cb)
class Printer:
    def __init__(s): s.r=Reactor(); s.objs={'gcode':Gcode(),'webhooks':W()}; s.handlers=[]
    def get_reactor(s): return s.r
    def lookup_object(s,n,d=None): return s.objs.get(n,d)
    def register_event_handler(s,e,cb): s.handlers.append((e,cb))
    def send_event(s,*a,**k): pass
class W:
    def register_mux_endpoint(s,*a,**k): pass
class Cfg:
    def __init__(s,p): s.p=p
    def get_printer(s): return s.p
    def get_name(s): return 'load_cell_probe'
    def getint(s,n,default=None,**k): return {'reference_tare_counts':345400}.get(n,default)
    def getfloat(s,n,default=None,**k): return {'counts_per_gram':95.783}.get(n,default)
    def getchoice(s,n,c,default=None): return c[default] if default is not None else 1.
    def get(s,n,default=None,**k): return default
    def getboolean(s,n,default=None,**k): return default
class Sensor:
    """Mimics BatchBulkHelper: truthy return keeps client, falsy removes."""
    def __init__(s): s.clients=[]
    def get_samples_per_second(s): return 320
    def get_range(s): return (-2**23,2**23-1)
    def get_status(s,e): return {}
    def add_client(s,cb): s.clients.append(cb)
    def emit(s,t0,n,counts):
        msg={'data':[(t0+i/320.0,counts) for i in range(n)],'errors':0,'overflows':0}
        for cb in list(s.clients):
            if not cb(msg): s.clients.remove(cb)

def build(probe_style):
    p=Printer(); sensor=Sensor()
    lc=lcmod.LoadCell(Cfg(p),sensor)
    for e,cb in p.handlers:
        if e=='klippy:ready': cb()
    for cb in p.r.cbs: cb(0)            # runs _handle_do_ready
    if probe_style:
        class Probe: pass
        pr=Probe(); pr._load_cell=lc; p.objs['load_cell_probe']=pr
    else:
        p.objs['load_cell']=lc
    return p,sensor,lc

for style in (False, True):
    p,sensor,lc=build(style)
    name='load_cell_probe' if style else 'load_cell'
    bus=SampleBus(p,name,60000,15.0)
    assert bus.available(), 'not found'
    got=[]; bus.subscribe(got.extend)
    # three batches of constant force: counts = tare + 95.783*10 -> 10 g
    for k in range(3):
        sensor.emit(1000+k*0.5,160,345400+957.83)
        p.r.t+=0.5
    assert bus.unit=='g', bus.unit
    assert len(got)==480, len(got)
    assert abs(got[0][1]-10.0)<1e-6, got[0]
    assert abs(got[0][0]-1000.0)<1e-9
    assert bus.running and len(lc.clients.client_cbs)==2 and len(sensor.clients)==1, (len(lc.clients.client_cbs), len(sensor.clients))
    # live cursor
    a=bus.live(0); b=bus.live(a['seq']); assert b['samples']==[]
    # unsubscribe + idle -> client must be released (falsy return) after live_idle
    bus.unsubscribe(got.extend)
    p.r.t+=30; sensor.emit(1002,10,345400)
    assert bus.running is False and len(lc.clients.client_cbs)==1, len(lc.clients.client_cbs)
    assert len(sensor.clients)==1  # Klipper's own sensor stream untouched
    # re-subscribe works again, no duplicates
    got2=[]; bus.subscribe(got2.extend); sensor.emit(1003,160,345400); assert len(got2)==160, len(got2)
    print('real-LoadCell OK, style=', 'load_cell_probe' if style else 'load_cell', 'unit', bus.unit)
