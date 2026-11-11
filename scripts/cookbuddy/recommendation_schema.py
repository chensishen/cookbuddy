"""Validated recommendation state and the vocabulary used by turn interpretation."""

INGREDIENTS=('鸡肉','牛肉','猪肉','鱼肉','鸡翅','鸡腿','鸡胸肉','虾','豆腐','土豆','茄子','黄瓜','番茄','西红柿','鸡蛋')
NUMBERS={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
EXCLUSION_ALIASES={'小米辣':'辣椒','小米椒':'辣椒','干辣椒':'辣椒','辣椒面':'辣椒','辣椒粉':'辣椒','辣椒油':'辣椒',
                   '芫荽':'香菜','花生米':'花生','花生油':'花生','西红柿':'番茄'}
EXCLUSION_NAMES=set(INGREDIENTS)|set(EXCLUSION_ALIASES)|{'辣椒','香菜','花生','葱','姜','蒜','青椒','红椒','胡椒','芝麻','牛奶','鸡蛋'}
RESPONSE_FIELDS={'description','reason','main_ingredients','missing_ingredients'}


def ingredient_names():
    from ingredient_recommendation import ingredient_vocabulary
    return set(INGREDIENTS)|ingredient_vocabulary()


def valid_state(state):
    if not isinstance(state,dict) or type(state.get('version')) is not int or state.get('version') not in (1,2) or state.get('kind')!='recommendation':return False
    if state.get('mode') not in ('preference','inventory'):return False
    for field in ('ingredients','tastes','excluded_methods','extra_constraints','excluded_ingredients'):
        values=state.get(field,[] if field=='excluded_ingredients' else None)
        if not isinstance(values,list) or len(values)>20 or any(not isinstance(v,str) or not v or len(v)>200 for v in values):return False
    if any(v not in EXCLUSION_NAMES for v in state.get('excluded_ingredients',[])):return False
    fields=state.get('response_fields',[])
    if not isinstance(fields,list) or any(not isinstance(v,str) or v not in RESPONSE_FIELDS for v in fields) or len(fields)>4:return False
    if type(state.get('limit')) is not int or not 1<=state['limit']<=10:return False
    choices=state.get('choices')
    return (isinstance(choices,list) and len(choices)<=10 and
            all(isinstance(c,dict) and set(c)=={'title','source'} and all(isinstance(v,str) and len(v)<=300 for v in c.values()) for c in choices))


# Bounded cooking-style vocabulary used as a positive filter on recipe titles
# and introductions. It is not an open-ended method parser.
STYLES=('凉拌','清蒸','红烧','炖','炒','烤','煎','炸','焖','煮')


def new_state():
    return {'version':2,'kind':'recommendation','mode':'preference','ingredients':[],
            'tastes':[],'excluded_methods':[],'excluded_ingredients':[],
            'extra_constraints':[],'response_fields':[],'limit':3,'choices':[]}
