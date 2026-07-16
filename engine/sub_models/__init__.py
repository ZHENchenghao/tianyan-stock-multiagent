"""子模型包 — 十五大战法（推背图5 + 美国6 + 中国4）"""
from . import (xuxiang, livermore, zhaolaoge, xiaocyu, yangjia,
               ptj, minervini, druckenmiller, darvas, loeb, wyckoff,
               beijingchaoshou, tuixue, qiaobangzhu, logicbro)

ALL_MODELS = {
    '徐翔': xuxiang, '利弗莫尔': livermore, '赵老哥': zhaolaoge,
    '小鳄鱼': xiaocyu, '炒股养家': yangjia,
    'PTJ': ptj, 'Minervini': minervini, 'Druckenmiller': druckenmiller,
    'Darvas': darvas, 'Loeb': loeb, 'Wyckoff': wyckoff,
    '北京炒家': beijingchaoshou, '退学炒股': tuixue,
    '乔帮主': qiaobangzhu, '逻辑哥': logicbro,
}
