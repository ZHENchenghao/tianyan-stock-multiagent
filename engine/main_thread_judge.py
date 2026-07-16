# -*- coding: utf-8 -*-
"""
天眼 v4.2 · 主线判断引擎 (PDF增强版)
====================================
完全基于PDF分析逻辑, 每日自动判断:
  1. 当日主线/次主线
  2. 主线所处阶段(启动/发酵/高潮/退潮)
  3. 需避雷的方向(二线AI/情绪地产/伪催化)
  4. 明日重点观察指标

PDF核心逻辑:
  - 地缘缓和→油价下行受益链 = 今日最强主线
  - AI硬件龙头业绩强化但交易拥挤 = 次主线
  - 人民币升值≠全面牛市 = 伪催化避雷
  - 等待美债现金市场第二确认 = 操作纪律

用法:
  judge = MainThreadJudge()
  result = judge.analyze()
  # → {main_thread, sub_thread, avoid_list, tomorrow_watch, strategy}
"""

import sys, os, json
from datetime import datetime, date, timedelta

os.environ['TQDM_DISABLE'] = '1'

import ssl
ssl._create_default_https_context = ssl._create_unverified_context
os.environ['PYTHONHTTPSVERIFY'] = '0'

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE, '..', 'main_thread.json')


class MainThreadJudge:
    """
    PDF主线判断引擎

    三步:
      1. 从联动网络读取当日宏观冲击+传导链+行业评分
      2. 按PDF规则判定主线/次主线/避雷/阶段
      3. 生成明日观察指标+一句话策略
    """

    def __init__(self):
        self.linkage = None
        self.shocks = []
        self.chains = []
        self.industry_scores = {}

    def _load_data(self):
        try:
            from engine.linkage_network import load_linkage
            self.linkage = load_linkage()
            self.shocks = self.linkage.get('shocks', [])
            self.chains = self.linkage.get('chains', [])
            self.industry_scores = self.linkage.get('industry_scores', {})
        except Exception:
            self.linkage = {}

    def _identify_main_thread(self) -> dict:
        """
        PDF规则: 主线判定
        优先级: 传导链信号 > 单一宏观冲击 > 动量延续
        """
        # 1. 检查是否有确认的传导链
        if self.chains:
            primary = self.chains[0]
            # 根据链类型确定主线行业
            chain_industry_map = {
                'geopolitical_easing': '油价回落受益链',
                'rate_relief': '利率缓和→成长修复',
                'inflation_scare': '再通胀→资源防御',
            }
            main_theme = chain_industry_map.get(
                primary.get('chain_key', ''), primary.get('label', '')
            )
            main_sectors = []
            for chain_name in primary.get('benefit', []):
                from engine.linkage_network import CHAIN_INDUSTRIES
                main_sectors.extend(CHAIN_INDUSTRIES.get(chain_name, []))

            return {
                'name': main_theme,
                'type': 'chain_driven',
                'chain': primary.get('label', ''),
                'sectors': list(set(main_sectors)),
                'confidence': 'high' if primary.get('strength', 0) >= 3 else 'medium',
                'stage': '启动→发酵' if primary.get('strength', 0) >= 2 else '早期启动',
                'driver': '海外映射+地缘+情绪',
                'note': primary.get('description', '')[:120],
            }

        # 2. 单冲击驱动
        if self.shocks:
            top = self.shocks[0]
            shock_theme_map = {
                'wti_drop': '油价暴跌→成本受益',
                'wti_surge': '油价暴涨→通胀防御',
                'us10y_drop': '利率下行→成长估值修复',
                'us10y_surge': '利率上行→价值防御',
                'usdcny_drop': '人民币升值→外资回流',
            }
            theme = shock_theme_map.get(top.get('shock_type', ''), top.get('shock_desc', '')[:30])

            # 从行业评分中找TOP受益行业
            bullish = sorted(
                [(k, v) for k, v in self.industry_scores.items() if v.get('score', 0) > 5],
                key=lambda x: x[1]['score'], reverse=True
            )[:5]

            return {
                'name': theme,
                'type': 'shock_driven',
                'chain': None,
                'sectors': [b[0] for b in bullish],
                'confidence': 'medium' if abs(top.get('z_score', 0)) >= 2.5 else 'low',
                'stage': '概念发酵期',
                'driver': '宏观冲击单变量',
                'note': '无传导链确认, 以单冲击信号为主',
            }

        # 3. 无冲击→看动量延续
        return {
            'name': '延续前日趋势',
            'type': 'momentum_continuation',
            'chain': None,
            'sectors': [],
            'confidence': 'low',
            'stage': '延续',
            'driver': '无新增宏观信息',
            'note': '无显著宏观冲击, 以技术面动量为准',
        }

    def _identify_sub_thread(self) -> dict:
        """PDF规则: 次主线=AI硬件龙头 (基本面持续, 交易拥挤)"""
        # PDF明确: AI主线没有坏, 但交易筹码变挤
        return {
            'name': 'AI硬件核心资产',
            'sectors': ['半导体', '电子元件', '通信设备'],
            'strategy': '只留龙头(光模块/PCB/先进封装), 不追二线扩散',
            'risk': '交易拥挤(科技仓位接近纪录高位), 易出预期差的是筹码结构不是业绩',
            'note': 'NVIDIA FY2027 Q1业绩+85%确认景气, 但Goldman/Reuters指向仓位拥挤',
        }

    def _identify_avoid_list(self) -> list:
        """PDF规则: 避雷清单"""
        avoid = []

        # 伪催化: 人民币升值≠地产链
        false_catalysts = self.linkage.get('false_catalysts', [])
        for fc in false_catalysts:
            avoid.append({
                'type': '伪催化',
                'sectors': fc.get('affected_sectors', []),
                'reason': fc.get('message', ''),
                'severity': 'high',
            })

        # 二线AI概念 (PDF明确: 不追无业绩支撑的题材扩散)
        avoid.append({
            'type': '二线题材',
            'sectors': ['AI概念(无订单小票)', '算力租赁(纯情绪)', '大模型(未商业化)'],
            'reason': 'AI主线只留龙头, 二线概念无订单兑现能力。基本面强不=全板块一起涨。',
            'severity': 'medium',
        })

        # 情绪化追涨方向
        avoid.append({
            'type': '情绪追涨',
            'sectors': ['地产链(情绪补涨)', '强顺周期(缺乏信用扩张)', '全面牛市逻辑'],
            'reason': '外部缓和≠内部周期反转。量能偏低(280亿)不支持全面牛市。',
            'severity': 'high',
        })

        # 地缘敏感资产 (如果WTI波动中)
        wti_volatile = any(
            s.get('var') == 'wti' and abs(s.get('z_score', 0)) >= 1.5
            for s in self.shocks
        )
        if wti_volatile:
            avoid.append({
                'type': '地缘反转',
                'sectors': ['油服', '油运', '纯资源防御'],
                'reason': '美伊谈判headline驱动, 消息随时反转。油价受益链仓位不宜一次打满。',
                'severity': 'medium',
            })

        return avoid

    def _tomorrow_watch(self) -> list:
        """PDF规则: 明日重点观察指标"""
        watch = []

        # 油价确认
        try:
            from engine.macro_shock_detector import _get_series
            wti = _get_series('wti')
            if wti is not None and len(wti) >= 2:
                last = float(wti.iloc[-1])
                watch.append({
                    'indicator': f'布油能否有效跌破95美元 (当前WTI≈${last:.1f})',
                    'why': 'PDF: 确认油价下行趋势, 决定"油价受益链"的持续性',
                    'threshold': '<$95确认趋势 / >$100反转预警',
                    'importance': 'critical',
                })
        except Exception:
            pass

        # 美债确认
        watch.append({
            'indicator': '美债现金市场重开后10Y是否回到4.50%下方',
            'why': 'PDF: 假日真空过后, 美债市场给出第二确认。若10Y重新站上4.55%→不是全面risk-on, 只是回到高利率常态',
            'threshold': '<4.50%利多成长 / >4.55%成长重新承压',
            'importance': 'critical',
        })

        # 人民币
        watch.append({
            'indicator': '离岸人民币能否稳住6.78一线',
            'why': 'PDF: 汇率强≠地产强, 但人民币稳在外资回流有利大盘核心资产',
            'threshold': '<6.78继续利多 / >6.85外资可能重新流出',
            'importance': 'high',
        })

        # 地缘表态
        watch.append({
            'indicator': '特朗普/伊朗关于霍尔木兹与协议时点的最新表态',
            'why': 'PDF: 市场已按"和平快速落地"定价, 公开口径未确认→短线交易拥挤度已上升',
            'threshold': '任何推迟/反复→油价可能单日反转5%+',
            'importance': 'high',
        })

        # 黄金确认
        watch.append({
            'indicator': '现货金能否维持在4600美元/盎司附近',
            'why': 'PDF: 黄金不深跌=市场未完全撤出避险。若金价跟随大跌→市场真正接受"地缘风险结束"',
            'threshold': '守住4600=风险溢价下降非风险消失 / 跌破=确认地缘溢价全部回吐',
            'importance': 'medium',
        })

        return watch

    def _strategy_conclusion(self) -> str:
        """PDF规则: 一句话策略"""
        chains_active = len(self.chains) > 0
        market_bias = self.linkage.get('summary', {}).get('market_bias', 'mixed')

        if chains_active:
            return (
                '先做油价下行的一阶受益和高低切换, '
                '不把一次地缘缓和交易误判成全面估值牛市。'
                'AI继续只拿核心, 不追扩散。'
                '等美债现金市场重开后给出第二确认再调整仓位。'
            )
        elif market_bias == 'risk_on':
            return '宏观信号偏积极, 但量能偏低需放量确认。优先持有核心资产, 不追高波动题材。'
        elif market_bias == 'risk_off':
            return '宏观信号偏防御, 减仓高估值成长, 增加现金和防御仓位。等待信号明朗。'
        else:
            return '市场信号中性, 维持现有仓位不动。短线以高低切换为主, 不新增仓位。'

    def analyze(self) -> dict:
        """主入口: 生成完整主线判断报告"""
        self._load_data()

        main = self._identify_main_thread()
        sub = self._identify_sub_thread()
        avoid = self._identify_avoid_list()
        watch = self._tomorrow_watch()
        strategy = self._strategy_conclusion()

        result = {
            'generated': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'main_thread': main,
            'sub_thread': sub,
            'avoid_list': avoid,
            'tomorrow_watch': watch,
            'strategy': strategy,
            'macro_summary': {
                'market_bias': self.linkage.get('summary', {}).get('market_bias', 'mixed'),
                'chain_count': len(self.chains),
                'shock_count': len(self.shocks),
                'false_catalyst_count': self.linkage.get('summary', {}).get('false_catalyst_count', 0),
            },
        }

        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        return result


def load_main_thread() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return MainThreadJudge().analyze()


if __name__ == '__main__':
    print('╔══════════════════════════════════╗')
    print('║  主线判断引擎 v4.2 (PDF增强)      ║')
    print('╚══════════════════════════════════╝')

    judge = MainThreadJudge()
    result = judge.analyze()

    mt = result['main_thread']
    print(f'\n=== 当日主线 ===')
    print(f'  主题: {mt["name"]}')
    print(f'  阶段: {mt["stage"]}')
    print(f'  置信度: {mt["confidence"]}')
    print(f'  行业: {mt["sectors"][:5]}')

    st = result['sub_thread']
    print(f'\n=== 次主线 ===')
    print(f'  主题: {st["name"]}')
    print(f'  策略: {st["strategy"]}')
    print(f'  风险: {st["risk"]}')

    print(f'\n=== 避雷清单 ===')
    for a in result['avoid_list']:
        print(f'  [{a["severity"]}] {a["type"]}: {a["reason"][:60]}...')

    print(f'\n=== 明日观察 ===')
    for w in result['tomorrow_watch']:
        print(f'  [{w["importance"]}] {w["indicator"][:60]}')

    print(f'\n=== 一句话策略 ===')
    print(f'  {result["strategy"]}')
