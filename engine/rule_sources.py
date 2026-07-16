"""天眼规则溯源库 — 86条规则出处+原文+量化依据 + 动态置信度 + 数据质量门禁
每条规则标注: 原始出处 | 原文引用 | A股修正说明 | 量化潜在偏差 | 存活状态 | 反馈历史
"""
import sys, os, json, math
from datetime import datetime, timedelta
from collections import defaultdict

# ═══════════════════════════════════════════
# 规则存活状态机
# ═══════════════════════════════════════════

RULE_STATES = {
    'active':     '活跃 — 正常发出信号',
    'probation':  '观察期 — 连续N个信号准确率下降，限制仓位上限50%',
    'frozen':     '冻结 — 准确率跌破阈值或触发宪法C1，禁止买入',
    'retired':    '退役 — 连续3月确认失效，信号仅供历史对比',
}

STATE_TRANSITIONS = {
    'active':    {'on_decay': 'probation', 'on_violation': 'frozen'},
    'probation': {'on_recover': 'active', 'on_worsen': 'frozen'},
    'frozen':    {'on_recover': 'probation', 'on_confirm_fail': 'retired'},
    'retired':   {'on_revival': 'probation'},  # 市场状态回归时可复活
}

# ═══════════════════════════════════════════
# 大师原始资料来源
# ═══════════════════════════════════════════

MASTER_SOURCES = {
    '徐翔': {
        'primary': [
            '泽熙投资2010-2015五年交易记录',
            '徐翔案(2015)判决书披露交易细节',
            '《徐翔:从3万到250亿》(中国证券报/财经)',
        ],
        'secondary': '媒体复盘+业内分析+交割单反推',
        'note': '徐翔本人无名著。规则来自事后复盘及庭审披露，非一手资料。'
    },
    '利弗莫尔': {
        'primary': [
            '《股票大作手回忆录》Edwin Lefevre, 1923',
            '《股票大作手操盘术》Jesse Livermore, 1940',
        ],
        'secondary': '《金融怪杰》相关引用',
        'note': '一手原文。A股修正：美股T+0无涨跌停→A股T+1有涨跌停。'
    },
    '赵老哥': {
        'primary': '淘股吧ID"赵老哥"2012-2016实盘交割单',
        'secondary': '《八年一万倍》复盘分析',
        'note': '实盘记录+社区复盘，非专著。涨停板规则A股原生适用。'
    },
    '小鳄鱼': {
        'primary': '淘股吧ID"小鳄鱼"实盘交割单',
        'secondary': '社区复盘+交割单分析',
        'note': '实盘记录。盈亏比1.12:1是隔日高频特征，非长线标准。'
    },
    '炒股养家': {
        'primary': '淘股吧"养家心法"全集(2010-2012)',
        'secondary': '《养家心法》网络整理版',
        'note': 'A股原创。情绪周期理论为养家独创，无英文对等概念。'
    },
    'PTJ': {
        'primary': [
            '《金融怪杰》(Market Wizards) Jack Schwager, 1989 — PTJ访谈',
            '《Trader》(纪录片) PBS, 1987',
        ],
        'secondary': 'Tudor Investment Corp投资信函',
        'note': '一手访谈。A股修正：200日均线在A股用60日线替代(持仓周期短)。'
    },
    'Minervini': {
        'primary': [
            '《超级绩效:金融怪杰交易之道》(Trade Like a Stock Market Wizard) Mark Minervini, 2013',
            '《Think and Trade Like a Champion》Mark Minervini, 2019',
        ],
        'secondary': 'SEPA策略线上课程/访谈',
        'note': '一手专著。A股修正：条件7幅度放宽至40%(A股波动率大)。'
    },
    'Druckenmiller': {
        'primary': [
            '《The New Market Wizards》Jack Schwager, 1992 — Druckenmiller访谈',
            '《金融怪杰》相关章节',
        ],
        'secondary': 'Duquesne Family Office季度13F文件',
        'note': '一手访谈。Druckenmiller无专著。'
    },
    'Darvas': {
        'primary': '《我如何在股市赚了200万美元》(How I Made $2,000,000 in the Stock Market) Nicolas Darvas, 1960',
        'secondary': '',
        'note': '一手自传。美股1950年代环境，A股箱体需修正涨停无量突破。'
    },
    'Loeb': {
        'primary': '《投资存亡战》(The Battle for Investment Survival) Gerald Loeb, 1935',
        'secondary': '',
        'note': '一手专著。写于大萧条深渊中，熊市智慧原生。'
    },
    'Wyckoff': {
        'primary': '《威科夫操盘法》(The Wyckoff Method) Richard Wyckoff, 1930s',
        'secondary': '《Trades About to Happen》David Weis(现代威科夫)',
        'note': '一手体系。Spring/SOS/UT术语为威科夫原创。'
    },
    '北京炒家': {
        'primary': [
            '淘股吧第37届再战杯实盘赛冠军(收益146.91%)',
            '淘股吧第9届百万杯季军(收益330.24%)',
        ],
        'secondary': '社区实盘帖+交割单分析',
        'note': '实盘比赛记录，非专著。'
    },
    '退学炒股': {
        'primary': '淘股吧ID"退学炒股"2017-2018实盘帖(5万→14个月150倍)',
        'secondary': '《退学炒股心法》网络整理版',
        'note': '实盘记录+心法自述。空仓理论为退学原创。'
    },
    '乔帮主': {
        'primary': '淘股吧ID"乔帮主"2012-2015交割单(42个月500倍)',
        'secondary': '蛇口游资席位分析',
        'note': '交割单分析为主。龙回头/下午板为乔帮主独创打法。'
    },
    '逻辑哥': {
        'primary': 'B站/公众号"逻辑哥"财经内容',
        'secondary': '',
        'note': '自媒体内容创作者。启动突破战法为个人总结，非学界验证。'
    },
}

# ═══════════════════════════════════════════
# 每条规则的溯源标注 + 存活状态
# ═══════════════════════════════════════════

RULE_SOURCES = {
    # ── 徐翔 R01-R07 ──
    'R01': {
        'master': '徐翔',
        'rule': '连3天跌停≥15+大盘缩量30日新低→冰点抄底20%',
        'source': '泽熙投资2015年股灾操作复盘',
        'original': '"断崖形下跌必有反弹"（徐翔接受媒体采访）',
        'quantification': '跌停15/连续3天/缩量30日新低：来自泽熙2015救市期实际入场时点反推',
        'ashare_note': 'A股特有：涨跌停制度下的极端情绪测量',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R02': {
        'master': '徐翔',
        'rule': '首次抄底亏损10%→无条件止损',
        'source': '徐翔风控纪律（媒体报道+庭审记录）',
        'original': '"止损要坚决——不同弹性个股设不同止损线"',
        'quantification': '10%来自泽熙产品净值回撤控制线',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R03': {
        'master': '徐翔',
        'rule': '首次抄底盈利10%→加倍买入至40%仓位',
        'source': '泽熙加仓模式复盘',
        'quantification': '10%浮盈→加倍是泽熙经典加仓节奏',
        'confidence': '低',
        'state': 'active',
        'feedback': [],
    },
    'R04': {
        'master': '徐翔',
        'rule': '妖股流通市值<50亿+无机构持仓→优先买入',
        'source': '泽熙小票偏好统计',
        'original': '"泽熙偏爱新兴行业小市值"（业内共识）',
        'quantification': '50亿阈值为A股小市值常用线，非泽熙明确声明',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R05': {
        'master': '徐翔',
        'rule': '新题材首次涨停潮→3天后关注龙头回调',
        'source': '徐翔题材炒作节奏',
        'original': '"选股三要素：政策支持+行业上升周期+基本面良好"',
        'quantification': '3天窗口来自徐翔操作节奏复盘',
        'confidence': '低',
        'state': 'active',
        'feedback': [],
    },
    'R06': {
        'master': '徐翔',
        'rule': '连3涨停后放量(量比>2)→减半仓',
        'source': '徐翔量比法则',
        'original': '"高位放量（量比>2）+涨幅<1%→放量滞涨，全部清仓"',
        'quantification': '量比>2直接来自徐翔原话',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R07': {
        'master': '徐翔',
        'rule': '持股满30天→无论盈亏全部卖出',
        'source': '泽投资金周转周期',
        'quantification': '30天为泽熙平均持股周期统计值',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },

    # ── 利弗莫尔 R08-R13 ──
    'R08': {
        'master': '利弗莫尔',
        'rule': '横盘30天+突破前高3%+放量2倍→买入15%',
        'source': '《股票大作手操盘术》第3章"关键点"',
        'original': '"当股票通过关键点后，就一直涨。……我总会在关键点买入。"',
        'quantification': '30天/3%/2倍：A股量化改编。利弗莫尔原话只说"关键点"和"放量"，未给精确数字。',
        'ashare_note': '利弗莫尔美股T+0→A股T+1，突破确认需3日站稳检查',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R09': {
        'master': '利弗莫尔',
        'rule': '突破后回调不跌破前高→加仓5%',
        'source': '《股票大作手操盘术》金字塔加仓法',
        'original': '"首仓后等待行情发展，如果首仓有利润，再逐步买入更多。利润会自己照顾自己。"',
        'quantification': '5%来自金字塔仓位分配比例反推',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R10': {
        'master': '利弗莫尔',
        'rule': '突破后3天不涨→止损卖出',
        'source': '《股票大作手回忆录》第5章',
        'original': '"如果股票没有按照我预期的方向运动，我就立即卖出。我不问为什么。"',
        'quantification': '3天为利弗莫尔原话中的时间窗口隐喻量化',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R11': {
        'master': '利弗莫尔',
        'rule': '上涨趋势中每次回调5%→加仓5%(最多3次)',
        'source': '《股票作手操盘术》金字塔法',
        'original': '"金字塔加仓——越涨越买，越买越少。"',
        'quantification': '5%+3次为利弗莫尔金字塔经典比例',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R12': {
        'master': '利弗莫尔',
        'rule': '跌破20日均线→全部卖出',
        'source': '利弗莫尔"关键点止损"体系',
        'original': '"跌破上一关键点即走。"',
        'quantification': '20日线为现代均线体系的利弗莫尔关键点替代',
        'ashare_note': '利弗莫尔用"关键价位"而非均线，20日线为A股适应性替代',
        'confidence': '低',
        'state': 'active',
        'feedback': [],
    },
    'R13': {
        'master': '利弗莫尔',
        'rule': '杠杆上限不超过2倍',
        'source': '《股票大作手回忆录》利弗莫尔爆仓教训',
        'original': '"永远不要使用超过你能承受的杠杆。"（利弗莫尔多次爆仓后的忠告）',
        'quantification': '2倍为后人总结，非利弗莫尔原话精确数字',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },

    # ── 赵老哥 R14-R20 ──
    'R14': {
        'master': '赵老哥',
        'rule': '新题材首板≥5只+封板<10:30+成交>5亿→入库关注',
        'source': '淘股吧赵老哥2014-2015交割单',
        'original': '"二板定龙头，一板能看出来个毛。"',
        'quantification': '5只/10:30/5亿来自交割单统计特征',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R15': {
        'master': '赵老哥',
        'rule': '二板封单≥10万手+换手10-20%+炸板≥5分钟+回封<14:00→打板25%',
        'source': '赵老哥"二板定龙头"核心打法',
        'original': '"二板是市场从分歧走向一致的过程。爆量换手=群众检验=选出真领袖。"',
        'quantification': '10万手/10-20%/5分钟/14:00均来自赵老哥交割单反推',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R16': {
        'master': '赵老哥',
        'rule': '二板炸板回封→加仓10%',
        'source': '赵老哥三种打板模式之一"高位强势反包"',
        'original': '"龙头第一根阴线后大阳线包住，造成反转。"',
        'quantification': '10%加仓比例为交割单反推',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R17': {
        'master': '赵老哥',
        'rule': '三板封单<5万手→次日开盘卖出',
        'source': '赵老哥万手封板术',
        'quantification': '5万手为赵老哥封板衰减阈值',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R18': {
        'master': '赵老哥',
        'rule': '次日低开3%→核按钮止损(五信号≥3触发)',
        'source': '赵老哥"核按钮止损"体系',
        'original': '"错了集合竞价割，马上进新状态。"',
        'quantification': '五信号体系/3%为交割单反推',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R19': {
        'master': '赵老哥',
        'rule': '主升浪中不做反抽；狂热(涨停跌停>8:1)→减仓30%',
        'source': '赵老哥12心法之"只做龙头主升"',
        'original': '"只做龙头主升，不做反抽、不做波动。"',
        'quantification': '减仓30%为琼斯宪法叠加规则，非赵老哥原话',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R20': {
        'master': '赵老哥',
        'rule': '持股满3天→全部卖出',
        'source': '赵老哥打板节奏',
        'original': '"纯打板节奏：第三日卖。"',
        'quantification': '3天来自赵老哥隔日超短节奏',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── 小鳄鱼 R21-R27（v2.1解冻: 动态C1, 胜率75%→最低盈亏比0.50:1） ──
    'R21': {
        'master': '小鳄鱼',
        'rule': '龙头首阴+换手≥25%→尾盘买入10%',
        'source': '小鳄鱼"隔日交易法"交割单',
        'original': '"隔日交易，快进快出——只有四个字：简单、纯粹。"',
        'quantification': '25%换手来自交割单统计',
        'confidence': '中',
        'state': 'active',  # v2.1解冻: 动态C1最低0.50:1, 实际1.12:1通过
        'feedback': [],
    },
    'R22': {
        'master': '小鳄鱼',
        'rule': '次日高开≥2%→15分钟全清',
        'source': '小鳄鱼"高开出局"路径一',
        'quantification': '2%/15分钟来自交割单反推',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R23': {
        'master': '小鳄鱼',
        'rule': '低开→开盘秒清',
        'source': '小鳄鱼"低开出局"路径三',
        'original': '"非龙头+次日不涨停→全清。隔日果断走。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R24': {
        'master': '小鳄鱼',
        'rule': '反包板封单≥5万手→打板买入',
        'source': '小鳄鱼"人气股反包"战法',
        'confidence': '中',
        'state': 'active',  # v2.1解冻
        'feedback': [],
    },
    'R25': {
        'master': '小鳄鱼',
        'rule': '点火30分钟不涨停→卖出',
        'source': '小鳄鱼离场条件',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R26': {
        'master': '小鳄鱼',
        'rule': '低吸后当日浮亏5%→次日止损',
        'source': '小鳄鱼止损纪律',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R27': {
        'master': '小鳄鱼',
        'rule': '每日交易上限→最多2次',
        'source': '小鳄鱼交易频率纪律',
        'confidence': '中',
        'state': 'active',  # v2.1解冻
        'feedback': [],
    },

    # ── 炒股养家 R28-R34 ──
    'R28': {
        'master': '炒股养家',
        'rule': '题材发酵首日+龙头一字板封单≥50万手→排板买入30%',
        'source': '养家心法·一字板排板篇',
        'original': '"题材发酵首日，龙头一字板封单超50万手，排队买入"',
        'quantification': '养家心法原文有明确数字',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R29': {
        'master': '炒股养家',
        'rule': '排板未成交→次日继续排(≤4天)',
        'source': '养家心法·排板替代法',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R30': {
        'master': '炒股养家',
        'rule': '开板后换手<15%+散户热度<5→继续持有',
        'source': '养家心法·格局持仓',
        'original': '"开板后换手率低于15%，继续持有。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R31': {
        'master': '炒股养家',
        'rule': '开板后换手>30%→减半仓',
        'source': '养家心法·格局持仓减仓条件',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R32': {
        'master': '炒股养家',
        'rule': '连3一字板后开板→全部卖出',
        'source': '养家心法·一字板退出',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R33': {
        'master': '炒股养家',
        'rule': '情绪退潮→空仓',
        'source': '养家心法·情绪周期篇',
        'original': '"退潮期：只卖不买，高低切换，防守。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R34': {
        'master': '炒股养家',
        'rule': '持股满12天→全部卖出',
        'source': '养家格局持仓周期',
        'quantification': '12天为养家平均格局周期，非固定值',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },

    # ── PTJ R35-R40 ──
    'R35': {
        'master': 'PTJ',
        'rule': '价<200日线→清仓转入防守',
        'source': '《金融怪杰》PTJ访谈',
        'original': '"判断任何市场健康状况最重要的指标，就是价格相对于200日均线的位置。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R36': {
        'master': 'PTJ',
        'rule': '月度亏损≥5%→当月停止交易',
        'source': '《金融怪杰》PTJ访问',
        'original': '"每个月最多亏掉总资产的5%。触及后当月立即停止交易。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R37': {
        'master': 'PTJ',
        'rule': '连续亏损时缩小仓位',
        'source': '《金融怪杰》',
        'original': '"当我连续亏损时，我会不断缩小仓位。当我交易最差的时候，仓位是最小的。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R38': {
        'master': 'PTJ',
        'rule': '流动性收紧→全线降仓',
        'source': 'PTJ宏观对冲框架',
        'original': '"关注流动性和央行政策来做市场择时。央行收水→全线降仓。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R39': {
        'master': 'PTJ',
        'rule': '宏观失衡+技术确认+200日线上→重押',
        'source': 'PTJ宏观对冲策略',
        'original': '"重大收益不是来自持续操作，来自对市场失衡产生的"高实质性机会"进行选择性大额押注。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R40': {
        'master': 'PTJ',
        'rule': '浮盈30-40%后→更激进(安全垫)',
        'source': '《金融怪杰》',
        'original': '"我会先慢慢赚，赚到30-40%的浮盈，然后如果有高确信机会，再追求100%的年度回报。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── Minervini R41-R47 ──
    'R41': {
        'master': 'Minervini',
        'rule': '8条件趋势模板≥7/8→可交易；≤6→回避',
        'source': '《超级绩效》第5章"趋势模板"',
        'original': '"8/8=Stage2确认，可交易。7/8=观察清单。≤6/8=回避。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R42': {
        'master': 'Minervini',
        'rule': 'VCP收缩≥33%+量缩至均量70%以下+放量突破Pivot→买点',
        'source': '《超级绩效》第6章"VCP收缩形态"',
        'original': '"盘整幅度≤前段盘整幅度的67%。成交量在收缩末端缩到20日均量的70%以下。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R43': {
        'master': 'Minervini',
        'rule': '距50日线>25%→延伸过高不买',
        'source': '《超级绩效》买入区',
        'original': '"买入区：距50日线0-15%。距50日线>25%=延伸过高=不买。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R44': {
        'master': 'Minervini',
        'rule': '放量跌破50或150日线→硬出场',
        'source': '《超级绩效》离场规则',
        'original': '"放量跌破50日线或150日线=趋势受损=硬出场。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R45': {
        'master': 'Minervini',
        'rule': '涨20-25%→减仓25-33%，剩余跟踪止盈',
        'source': '《超级绩效》止盈规则',
        'original': '"涨20-25%后减仓25-33%，剩余用10日线或21日EMA跟踪止盈。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R46': {
        'master': 'Minervini',
        'rule': '盈亏比<2:1→不开仓',
        'source': '《超级绩效》风控',
        'original': '"盈亏比低于2:1不开仓。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R47': {
        'master': 'Minervini',
        'rule': '熊市→100%现金，不做任何交易',
        'source': '《超级绩效》熊市策略',
        'original': '"熊市期间不交易的艺术——什么都不做往往是最好的决定。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── Druckenmiller R48-R52 ──
    'R48': {
        'master': 'Druckenmiller',
        'rule': '央行放水→进攻；收水→降仓',
        'source': '《The New Market Wizards》Druckenmiller访谈',
        'original': '"盈利不驱动整体市场，驱动市场的是美联储。关注流动性和央行政策来做市场择时。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R49': {
        'master': 'Druckenmiller',
        'rule': '浮盈30-40%后才能激进',
        'source': '《The New Market Wizards》',
        'original': '"先慢慢赚，赚到30-40%的浮盈，然后去追求100%年度回报。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R50': {
        'master': 'Druckenmiller',
        'rule': '集中1-2高确信机会(不分散)',
        'source': '《The New Market Wizards》',
        'original': '"分散化是投资中最误导人的概念。把鸡蛋放一个篮子里，看好这个篮子。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R51': {
        'master': 'Druckenmiller',
        'rule': '流动性拐点→最重要信号',
        'source': '《The New Market Wizards》',
        'original': '"流动性拐点出现是最重要的信号。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R52': {
        'master': 'Druckenmiller',
        'rule': '确信度+宏观+技术共振→重仓出击50%',
        'source': 'Druckenmiller集中重押框架',
        'original': '"重要的不是你对不对，而是你对的时候赚多少。"',
        'confidence': '低',
        'state': 'active',
        'feedback': [],
    },

    # ── Darvas R53-R57 ──
    'R53': {
        'master': 'Darvas',
        'rule': '连续3天不破高点=箱顶；3天不破低=箱底',
        'source': '《我如何在股市赚了200万》第7章"箱体理论"',
        'original': '"箱顶：连续3天不突破该高点。箱底：连续3天不跌破该低点。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R54': {
        'master': 'Darvas',
        'rule': '放量突破箱顶→买入，止损=箱底',
        'source': '《我如何在股市赚了200万》',
        'original': '"箱体突破就像端庄的贵妇突然跳到桌子上跳野舞——说明有什么变了，你必须关注。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R55': {
        'master': 'Darvas',
        'rule': '跌破箱底→止损出局',
        'source': '《我如何在股市赚了200万》箱体规则',
        'original': '"跌破箱底必走——不犹豫不幻想。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R56': {
        'master': 'Darvas',
        'rule': '形成新高箱体→金字塔加仓',
        'source': '《我如何在股市赚了200万》',
        'original': '"止损线随新箱体向上移动。只在形成新高位时加仓。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R57': {
        'master': 'Darvas',
        'rule': '大盘下跌→不做任何买入',
        'source': '《我如何在股市赚了200万》熊市法则',
        'original': '"大盘下跌时不做任何买入——箱体在熊市中反复假突破止损。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── Loeb R58-R62 ──
    'R58': {
        'master': 'Loeb',
        'rule': '统领理由被违反→立即卖出，不问价格',
        'source': '《投资存亡战》第3章',
        'original': '"统领理由一旦被违反，立即卖出，不问价格。被套牢拿住不放比随机卖出更糟糕。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R59': {
        'master': 'Loeb',
        'rule': '止损: 正常市场10%/熊市3%',
        'source': '《投资存亡战》止损章节',
        'original': '"正常市场止损上限10%。熊市/困难市场止损缩到3%。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R60': {
        'master': 'Loeb',
        'rule': '现金为王→等真正机会，不为"保持投资"而买',
        'source': '《投资存亡战》"现金的重要性"',
        'original': '"愿意并能够长期持有闲置现金，等待真正的机会，是投资生存战中成功的关键。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R61': {
        'master': 'Loeb',
        'rule': '集中1-4只(不分散)',
        'source': '《投资存亡战》集中投资',
        'original': '"最大的安全性，在于把所有鸡蛋放一个篮子里，看好这个篮子。有经验的投资者持有1-4只。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R62': {
        'master': 'Loeb',
        'rule': '买入前写：为什么买/预期涨多少/最多等多久/最多亏多少',
        'source': '《投资存亡战》"统领理由"系统',
        'original': '"买入前必须写下：1.为什么买？2.预期涨多少？3.最多等多久？4.最多亏多少？"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── Wyckoff R78-R82 ──
    'R78': {
        'master': 'Wyckoff',
        'rule': '价涨量增→健康；放量滞涨→主力出货',
        'source': '《威科夫操盘法》"努力vs结果"定律',
        'original': '"价涨量增=健康上涨。放量滞涨=努力无结果=抛压出现。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R79': {
        'master': 'Wyckoff',
        'rule': 'Spring弹簧效应→假跌破+快速拉回→供应耗尽→买入',
        'source': '《威科夫操盘法》吸筹模型C阶段',
        'original': '"Spring：假跌破支撑后快速拉回，确认供应耗尽。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R80': {
        'master': 'Wyckoff',
        'rule': 'SOS强势信号→带量突破→加仓',
        'source': '《威科夫操盘法》吸筹模型D阶段',
        'original': '"SOS(Sign of Strength)：带量突破，需求完全控制市场。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R81': {
        'master': 'Wyckoff',
        'rule': 'UT上冲回落→假突破+需求耗尽→清仓',
        'source': '《威科夫操盘法》派发模型C阶段',
        'original': '"UTAD(Upthrust After Distribution)：假突破阻力后快速回落，确认需求耗尽。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R82': {
        'master': 'Wyckoff',
        'rule': '努力vs结果背离→趋势不可持续',
        'source': '《威科夫操盘法》三大定律之"努力vs结果"',
        'original': '"价小涨量大=抛压出现。量价背离=趋势不可持续。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── 北京炒家 R63-R68 ──
    'R63': {
        'master': '北京炒家',
        'rule': '流通市值20-50亿+量比≥3+封单>流通市值1%→候选',
        'source': '淘股吧再战杯/百万杯实盘记录',
        'original': '"20-50亿流通市值+量比≥3+封单>流通市值1%=标准候选。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R64': {
        'master': '北京炒家',
        'rule': '板块涨停≥3只→板块效应确认',
        'source': '北京炒家题材筛选法',
        'original': '"板块内涨停≥3家形成板块效应。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R65': {
        'master': '北京炒家',
        'rule': '换手板6-8%区间震荡>30分钟→首选',
        'source': '北京炒家打板类型-换手板',
        'original': '"换手板：股价在6-8%区间震荡超30分钟消化抛压后上板。这是首选。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R66': {
        'master': '北京炒家',
        'rule': '低开→反弹分时高点必出，无论盈亏',
        'source': '北京炒家卖出铁律',
        'original': '"低开→反弹分时高点必出，无论盈亏。不追求卖在最高点。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R67': {
        'master': '北京炒家',
        'rule': '单票上限按资金规模分仓',
        'source': '北京炒家仓位管理体系',
        'original': '<80万满仓1-2只/80-500万2-4仓/500-2000万严格4仓/>2000万8仓防御',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R68': {
        'master': '北京炒家',
        'rule': '退潮期(跌停>20家)→仓位≤20%或空仓',
        'source': '北京炒家情绪周期适配',
        'original': '"退潮期（跌停>20家）→仓位≤20%或空仓。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── 退学炒股 R69-R72 ──
    'R69': {
        'master': '退学炒股',
        'rule': '回撤线机制: 资金在线上→全仓；触及→分仓',
        'source': '淘股吧退学炒股实盘帖',
        'original': '"资金在回撤线之上→全仓。触及回撤线→分仓。回撤线约10%幅度。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R70': {
        'master': '退学炒股',
        'rule': '三种冲动情境→禁止交易',
        'source': '退学炒股心法-三种最易冲动操作',
        'original': '"1.看到别人赚钱时 2.连续成功后 3.大亏后想扳本——这三种最易冲动操作。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R71': {
        'master': '退学炒股',
        'rule': '连亏→空仓反思，不报复交易',
        'source': '退学炒股心法',
        'original': '"错了就割，千万不要抱有任何幻想。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R72': {
        'master': '退学炒股',
        'rule': '行情好多做，行情不好少做',
        'source': '退学炒股核心语录',
        'original': '"行情好的时候多做，行情不好的时候少做。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── 乔帮主 R73-R77 ──
    'R73': {
        'master': '乔帮主',
        'rule': '龙头首碰5/10日线→龙回头低吸',
        'source': '乔帮主四大战法之"龙头首碰5/10日线低吸"',
        'original': '"龙头首次回调触碰5日线或10日线附近企稳→低吸。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R74': {
        'master': '乔帮主',
        'rule': '换手龙首阴反包+次日弱转强→介入',
        'source': '乔帮主四大战法之"强势龙头首阴反包"',
        'original': '"必须是换手龙（非一字板吃独食）。前日假阴线+次日弱转强封板→介入。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R75': {
        'master': '乔帮主',
        'rule': '尾盘封板(下午板)→全天换手充分→打板',
        'source': '乔帮主四大战法之"尾盘封板"',
        'original': '"偏爱下午2点后甚至尾盘封板。全天充分换手，炸板率极低。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R76': {
        'master': '乔帮主',
        'rule': '高位横盘换手板(8-9%震仓)→打板',
        'source': '乔帮主四大战法之"高位横盘换手板"',
        'original': '"股价在8-9个点高位长时间横盘震仓。该抛的筹码已全抛出，封板确定性极高。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },
    'R77': {
        'master': '乔帮主',
        'rule': '主升才是王道→不参与调整→全清',
        'source': '乔帮主核心交易理念',
        'original': '"主升才是王道。一定不能参与调整段。看不清楚→卖；看清楚了→追回。"',
        'confidence': '高',
        'state': 'active',
        'feedback': [],
    },

    # ── 逻辑哥 R83-R86 ──
    'R83': {
        'master': '逻辑哥',
        'rule': '平台整理≥10日+放量突破平台高×1.02+MACD金叉→买入',
        'source': '逻辑哥"启动突破战法"(B站/公众号)',
        'original': '"平台整理≥10日+放量突破平台高点×1.02+MACD金叉确认=买入信号。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R84': {
        'master': '逻辑哥',
        'rule': '三维共振(技术面+资金面+逻辑面)→加仓',
        'source': '逻辑哥三维分析框架',
        'original': '"技术面+逻辑面+资金面三重确认。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
    'R85': {
        'master': '逻辑哥',
        'rule': '分批建仓→首仓10%，确认后再加',
        'source': '逻辑哥仓位管理',
        'original': '"固定仓位分批建仓，组合分散持有。"',
        'confidence': '低',
        'state': 'active',
        'feedback': [],
    },
    'R86': {
        'master': '逻辑哥',
        'rule': '趋势走弱+破位关键支撑→纪律止损',
        'source': '逻辑哥离场条件',
        'original': '"趋势走弱（破位关键支撑）→纪律止损。"',
        'confidence': '中',
        'state': 'active',
        'feedback': [],
    },
}

# ═══════════════════════════════════════════
# L0 动态置信度引擎
# ═══════════════════════════════════════════

CONFIDENCE_WEIGHTS = {'高': 1.0, '中': 0.55, '低': 0.15}

def get_base_confidence(rule_id):
    """获取原始静态置信度权重"""
    src = RULE_SOURCES.get(rule_id, {})
    return CONFIDENCE_WEIGHTS.get(src.get('confidence', '低'), 0.15)

def get_dynamic_confidence(rule_id):
    """动态置信度 = 静态权重 × 回测因子 × 实战准确率因子

    回测因子: 基于Walk-Forward超额收益方向 (0.7~1.3)
    实战因子: 基于live_tracker信号准确率 (0.5~1.5)
    """
    src = RULE_SOURCES.get(rule_id, {})
    base = CONFIDENCE_WEIGHTS.get(src.get('confidence', '低'), 0.15)

    # 回测因子——从feedback中提取backtest结果
    backtest_factor = 1.0
    for fb in src.get('feedback', []):
        if fb.get('source') == 'backtest':
            if fb.get('result') == 'confirmed':
                backtest_factor = 1.3
            elif fb.get('result') == 'rejected':
                backtest_factor = 0.7
            elif fb.get('result') == 'inconclusive':
                backtest_factor = 0.9

    # 实战因子——从feedback中提取live_tracker准确率
    live_factor = 1.0
    live_accs = [fb.get('accuracy', 0) for fb in src.get('feedback', [])
                 if fb.get('source') == 'live_tracker']
    if live_accs:
        avg_acc = sum(live_accs) / len(live_accs)
        live_factor = max(0.5, min(1.5, avg_acc / 0.5))

    dynamic = base * backtest_factor * live_factor
    return round(max(0.05, min(1.0, dynamic)), 3)

def update_confidence(rule_id, source, result, accuracy=None, note=''):
    """接收回测/实战反馈，更新动态置信度

    source: 'backtest' | 'live_tracker' | 'audit' | 'manual'
    result: 'confirmed' | 'rejected' | 'inconclusive' | 'decay_detected'
    """
    if rule_id not in RULE_SOURCES:
        return False

    fb = {
        'source': source,
        'result': result,
        'accuracy': accuracy,
        'note': note,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    RULE_SOURCES[rule_id].setdefault('feedback', []).append(fb)

    # 自动状态转移
    state = RULE_SOURCES[rule_id].get('state', 'active')
    if result == 'rejected' and state == 'active':
        RULE_SOURCES[rule_id]['state'] = 'probation'
        fb['state_change'] = 'active→probation'
    elif result == 'decay_detected' and state in ('active', 'probation'):
        RULE_SOURCES[rule_id]['state'] = 'frozen'
        fb['state_change'] = f'{state}→frozen'
        RULE_SOURCES[rule_id]['freeze_reason'] = f'衰减检测: {note}'
    elif result == 'confirmed' and state == 'probation':
        RULE_SOURCES[rule_id]['state'] = 'active'
        fb['state_change'] = 'probation→active'

    return True

# ═══════════════════════════════════════════
# L0 数据质量门禁
# ═══════════════════════════════════════════

REQUIRED_FIELDS_BY_RULE_TYPE = {
    '打板': ['symbol', 'price', 'volume', 'board_seal_vol', 'turnover_rate', 'board_time'],
    '排板': ['symbol', 'price', 'one_word_seal_vol', 'queue_position'],
    '趋势': ['symbol', 'price', 'ma50', 'ma150', 'ma200', 'volume'],
    '低吸': ['symbol', 'price', 'daily_change_pct', 'turnover_rate', 'ma5', 'ma10'],
    '宏观': ['us10y', 'wti', 'usdcny', 'shibor_on', 'limit_up_count', 'limit_down_count'],
    '情绪': ['limit_up_count', 'limit_down_count', 'bomb_rate', 'consecutive_max', 'promotion_rate'],
}

def check_data_quality(rule, market_data, stock_data=None):
    """数据质量门禁：检查规则所需的输入数据是否齐全

    返回: (passed: bool, missing_fields: list, degraded: bool)
    """
    rule_id = rule.get('rule_id', '')
    action = rule.get('action', '')
    missing = []

    # 判断规则类型
    rule_type = '趋势'
    if action in ('打板', '排板'):
        rule_type = action
    elif action in ('买入', '加仓') and rule.get('position', 0) < 0.15:
        rule_type = '低吸'
    elif rule.get('master', '') in ('PTJ', 'Druckenmiller'):
        rule_type = '宏观'
    elif rule.get('master', '') in ('炒股养家', '退学炒股'):
        rule_type = '情绪'

    required = REQUIRED_FIELDS_BY_RULE_TYPE.get(rule_type, ['symbol', 'price'])

    for field in required:
        if market_data.get(field) is None and (stock_data or {}).get(field) is None:
            missing.append(field)

    passed = len(missing) == 0
    degraded = len(missing) <= 2  # 缺2个以内可降级

    return passed, missing, degraded

# ═══════════════════════════════════════════
# 溯源统计
# ═══════════════════════════════════════════

def source_stats():
    stats = {
        'total_rules': len(RULE_SOURCES),
        'confidence': {'高': 0, '中': 0, '低': 0},
        'states': {'active': 0, 'probation': 0, 'frozen': 0, 'retired': 0},
    }
    for rid, s in RULE_SOURCES.items():
        stats['confidence'][s.get('confidence', '低')] += 1
        stats['states'][s.get('state', 'active')] += 1
    return stats

def get_rules_by_state(state='active'):
    return [rid for rid, s in RULE_SOURCES.items() if s.get('state') == state]

def get_rules_by_master(master):
    return [rid for rid, s in RULE_SOURCES.items() if s.get('master') == master]

def print_source_report():
    stats = source_stats()
    print(f"\n{'='*60}")
    print(f"  天眼规则溯源+存活状态报告")
    print(f"{'='*60}")
    print(f"  总规则: {stats['total_rules']}条")
    print(f"\n  置信度:")
    for level, count in stats['confidence'].items():
        print(f"    {level}: {count}条")
    print(f"\n  存活状态:")
    for state, count in stats['states'].items():
        desc = RULE_STATES.get(state, '')
        print(f"    {state}({desc}): {count}条")
    print(f"\n  动态置信度示例(R01/R08/R14):")
    for rid in ['R01', 'R08', 'R14']:
        base = get_base_confidence(rid)
        dyn = get_dynamic_confidence(rid)
        s = RULE_SOURCES[rid]
        print(f"    {rid} [{s['master']}]: 静态{base:.0%} → 动态{dyn:.0%} (状态:{s['state']})")


def enrich_rules_with_source(rules):
    """给每条规则自动附加溯源+动态置信度+存活状态"""
    for r in rules:
        rid = r.get('rule_id', '')
        if rid in RULE_SOURCES:
            src = RULE_SOURCES[rid]
            r['source'] = src.get('source', '?')
            r['original'] = src.get('original', '?')
            r['confidence'] = src.get('confidence', '?')
            r['ashare_note'] = src.get('ashare_note', '')
            r['state'] = src.get('state', 'active')
            r['freeze_reason'] = src.get('freeze_reason', '')
            r['dynamic_confidence'] = get_dynamic_confidence(rid)
    return rules


if __name__ == '__main__':
    print_source_report()
    # 测试反馈更新
    update_confidence('R08', 'backtest', 'confirmed', accuracy=0.72, note='WF回测通过')
    print(f"\n  反馈后R08动态置信度: {get_dynamic_confidence('R08'):.0%}")
