"""Tests for the page-facing backend: printer status, saving settings to
pa_loadcell.cfg, custom themes. Run: python3 tests/test_ui_backend.py"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'klipper'))
import harness as h
from pa_loadcell import cfgfile, themes
from pa_loadcell.errors import PAError


def call(p, name, **params):
    r = h.Req(params)
    p.objs['webhooks'].eps['pa_loadcell/' + name](r)
    return r.out


def test_live_has_printer_status():
    p, plug = h.build()
    st = call(p, 'live', since=0)['printer']
    assert set(st) >= {'temp', 'target', 'z'} and st['z'] == 20.0, st


def test_config_save_writes_file_and_defaults():
    p, plug = h.build()
    out = call(p, 'config_save', values={'fast_flow': 15, 'cycles': 8,
                                         'filament': 'ASA CF', 'skip_first': 0})
    text = open(os.path.join(p.cfgdir, 'pa_loadcell.cfg')).read()
    assert 'fast_flow: 15 ' in text and 'cycles: 8 ' in text, text
    assert 'filament: ASA CF' in text and 'skip_first_pulse: 0' in text
    assert '# mm3/s, fast leg' in text            # comment kept
    assert 'min_temp: 200' in text                         # safety untouched
    assert plug.defaults.fast_flow == 15.0 and plug.defaults.cycles == 8
    assert out['defaults']['fast_flow'] == 15.0
    assert os.path.isfile(os.path.join(p.cfgdir, 'pa_loadcell.cfg.bak-pa_loadcell-ui'))
    assert call(p, 'defaults')['fast_flow'] == 15.0         # page sees it
    for bad in ({'min_temp': 150}, {'slow_flow': 50}, {'fast_flow': 'x'},
                {'cycles': 1}, {'filament': 'a#b'}):
        failed = False
        try: call(p, 'config_save', values=bad)
        except Exception: failed = True
        assert failed, bad
    assert plug.defaults.fast_flow == 15.0                  # bad saves change nothing


def test_update_text_only_touches_section():
    t = '[a]\nfast_flow: 1\n[pa_loadcell]\nfast_flow: 2  # c\n\n[b]\nx: 1\n'
    n = cfgfile.update_text(t, {'fast_flow': 3, 'cycles': 6})
    assert n.startswith('[a]\nfast_flow: 1\n[pa_loadcell]\nfast_flow: 3  # c\ncycles: 6\n'), n
    try: cfgfile.update_text('[x]\n', {'a': 1}); assert False
    except PAError: pass


def test_themes_save_list_delete():
    p, plug = h.build()
    t = {'format': 'piro_edge_theme', 'name': 'Ocean Blue', 'base': 'fluidd',
         'mode': 'dark', 'tokens': {'accent': '#1e88e5', 'bg': '#0d1117',
                                    'radius': '8px', 'font': 'Inter, sans-serif',
                                    'cmd': '#ffb74d'}}
    saved = call(p, 'theme_save', theme=t)['theme']
    assert saved['id'] == 'ocean-blue' and saved['base'] == 'fluidd'
    assert saved['tokens']['cmd'] == '#ffb74d'
    term = call(p, 'theme_save', theme={'name': 'Crimson', 'base': 'terminal', 'tokens': {}})['theme']
    assert term['base'] == 'terminal' and term['id'] == 'crimson'
    call(p, 'theme_delete', id='crimson')
    lst = call(p, 'theme_list')['themes']
    assert [x['name'] for x in lst] == ['Ocean Blue']
    call(p, 'theme_delete', id='ocean-blue')
    assert call(p, 'theme_list')['themes'] == []
    for bad in (dict(t, base='nope'), dict(t, tokens={'bg': 'url(x)'}),
                dict(t, tokens={'bg': 'red;}body{'}), dict(t, tokens={'x': '#fff'}),
                dict(t, name=''), {'format': 'other'}, 'text'):
        try: themes.clean(bad); assert False, bad
        except PAError: pass
    # a custom theme cannot replace a built-in one
    assert call(p, 'theme_save', theme=dict(t, name='PIRO'))['theme']['id'] == 'piro-custom'


if __name__ == '__main__':
    for n, fn in list(globals().items()):
        if n.startswith('test_'):
            fn(); print('ok', n)
