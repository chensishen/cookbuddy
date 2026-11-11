"""Conservative normalization; digits inside quantities and ordinals stay intact."""
import re
import unicodedata
from functools import lru_cache
from .config import settings

@lru_cache(maxsize=1)
def converter():
    from opencc import OpenCC
    return OpenCC('t2s')


def normalize(text):
    cfg=settings()['normalize'];original=text
    if cfg['nfkc']:text=unicodedata.normalize('NFKC',text)
    if cfg['t2s']:text=converter().convert(text)
    text=re.sub(r'\s+',' ',text).strip()
    text=text.translate(str.maketrans({'，':',','；':';','：':':','。':'.','！':'!','？':'?'}))
    ignored=[]
    if cfg['strip_trailing_noise']:
        while text and (unicodedata.category(text[-1]) in ('So','Sk') or text[-1]=='\ufe0f'):
            ignored.insert(0,text[-1]);text=text[:-1].rstrip()
        text=text.rstrip('.!?;, ')
        # A digit after a *completed* ordinal or sentence is orphaned. Bare
        # digits, measurements, dates, temperatures and ordinal numbers remain.
        m=re.search(r'(?:(?<=道)|(?<=个)|(?<=号)|(?<=[.!?]))\s*(\d{1,2})$',text)
        if m and (re.search(r'第[一二两三四五六七八九十\d ]+(?:道|个|号)',text[:m.start()]) or text[:m.start()].endswith(('.', '!', '?'))):
            ignored.append(m[1]);text=text[:m.start()].rstrip('.!? ')
    return {'original':original,'text':text,'ignored_tokens':ignored}
