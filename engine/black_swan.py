# -*- coding: utf-8 -*-
"""
黑天鹅场景库 + 压力测试引擎 v1.0
===============================
问题: 黑天鹅场景库尚未建立
方案: 20个典型黑天鹅事件 + 自动压力测试 + 应对预案

学术依据:
  - Taleb (2007) 《黑天鹅》: 极端事件不可预测但必须准备
  - 巴塞尔协议III: 压力测试框架
  - 中国银保监《商业银行压力测试指引》(2020)
  - Kaminski & Lo (2014) 止损有效性

用法:
  bse = BlackSwanEngine(portfolio)
  report = bse.stress_test_all()
  plan = bse.contingency_plan('中美金融脱钩')
"""
import sys, os, json, math

BASE = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════
# 20+黑天鹅场景库
# ═══════════════════════════════════════════

BLACK_SWAN_SCENARIOS = {
    # === 国际宏观 (6个) ===
    '中美金融脱钩升级': {
        'category': '国际宏观',
        'trigger': '美国将中国列入汇率操纵国+加征60%关税+限制中资股在美上市',
        'impact': {'沪深300': -0.15, '有色': -0.20, '电力': -0.05, '电池': -0.18, '黄金': +0.12, '现金': 0},
        'duration_days': 60,
        'probability': 0.03,
        'historical_reference': '2018贸易战升级, 上证跌24%',
        'early_signals': ['美10Y急升>30bp', '人民币单日贬>1%', '北向资金连续3日净流出>100亿'],
    },
    '美联储意外加息100bp': {
        'category': '国际宏观',
        'trigger': '通胀失控, 美联储紧急加息100bp, 全球资产重定价',
        'impact': {'沪深300': -0.12, '有色': -0.15, '电力': -0.03, '电池': -0.14, '黄金': -0.05, '现金': +0.01},
        'duration_days': 30,
        'probability': 0.05,
        'historical_reference': '2022年美联储连续加息75bp, 纳指跌33%',
        'early_signals': ['美10Y连续3周>5.0%', 'VIX>30', '美联储鹰派讲话'],
    },
    '全球流动性危机': {
        'category': '国际宏观',
        'trigger': '类似2008年雷曼时刻, 大型金融机构倒闭, 信用市场冻结',
        'impact': {'沪深300': -0.30, '有色': -0.35, '电力': -0.15, '电池': -0.30, '黄金': -0.10, '现金': 0},
        'duration_days': 90,
        'probability': 0.01,
        'historical_reference': '2008年金融危机, 上证跌72%',
        'early_signals': ['LIBOR-OIS利差>100bp', '信用利差飙升', '银行股暴跌>10%/日'],
    },
    '台海/南海军事冲突': {
        'category': '地缘政治',
        'trigger': '台海或南海爆发军事冲突, 全球避险情绪飙升',
        'impact': {'沪深300': -0.25, '有色': -0.20, '电力': -0.10, '电池': -0.22, '黄金': +0.15, '现金': 0},
        'duration_days': 45,
        'probability': 0.02,
        'historical_reference': '2022年俄乌冲突, 俄罗斯RTS指数单日跌39%',
        'early_signals': ['军工股异动', '黄金VIX飙升', '美国航母动向'],
    },
    '新兴市场债务危机': {
        'category': '国际宏观',
        'trigger': '新兴市场国家主权违约, 传染至A股',
        'impact': {'沪深300': -0.18, '有色': -0.22, '电力': -0.08, '电池': -0.15, '黄金': +0.05, '现金': 0},
        'duration_days': 40,
        'probability': 0.04,
        'historical_reference': '1997亚洲金融危机, 恒生跌60%',
        'early_signals': ['新兴市场ETF资金流出', 'CDS利差扩大', 'MSCI新兴市场指数破位'],
    },
    '全球疫情新变种': {
        'category': '公共卫生',
        'trigger': '新变种免疫逃逸+传播力增强, 全球重新封锁',
        'impact': {'沪深300': -0.10, '有色': -0.12, '电力': -0.05, '电池': -0.08, '黄金': +0.03, '现金': 0},
        'duration_days': 30,
        'probability': 0.06,
        'historical_reference': '2020年新冠疫情, 上证跌14.6%后反弹',
        'early_signals': ['各国新增病例数', 'WHO紧急声明', '航空股暴跌'],
    },

    # === 中国本土 (7个) ===
    'A股流动性枯竭': {
        'category': '中国本土',
        'trigger': '成交额萎缩至5000亿以下, 大面积跌停, 融资盘爆仓',
        'impact': {'沪深300': -0.20, '有色': -0.25, '电力': -0.10, '电池': -0.22, '黄金': +0.05, '现金': 0},
        'duration_days': 25,
        'probability': 0.05,
        'historical_reference': '2015年股灾, 千股跌停, 流动性枯竭',
        'early_signals': ['跌停家数>100', '炸板率>40%', '融资余额连续下降'],
    },
    '房地产硬着陆': {
        'category': '中国本土',
        'trigger': '头部房企债务违约, 银行系统坏账飙升, 房价暴跌30%',
        'impact': {'沪深300': -0.22, '有色': -0.28, '电力': -0.08, '电池': -0.15, '黄金': +0.08, '现金': 0},
        'duration_days': 60,
        'probability': 0.04,
        'historical_reference': '2021-2023年恒大/碧桂园危机',
        'early_signals': ['房企美元债暴跌', '银行股持续阴跌', '地产信托违约'],
    },
    '地方债务危机': {
        'category': '中国本土',
        'trigger': '地方政府融资平台大面积违约, 城投债暴跌',
        'impact': {'沪深300': -0.15, '有色': -0.18, '电力': -0.10, '电池': -0.12, '黄金': +0.05, '现金': 0},
        'duration_days': 40,
        'probability': 0.05,
        'historical_reference': '无直接先例(中国地方政府从未违约), 参考阿根廷',
        'early_signals': ['城投债信用利差扩大', '地方政府卖地收入骤降', '财政部特殊再融资债券'],
    },
    '量化交易监管升级': {
        'category': '中国本土',
        'trigger': '监管叫停量化程序化交易, 限制融券/高频/日内回转',
        'impact': {'沪深300': -0.08, '有色': -0.10, '电力': -0.03, '电池': -0.10, '黄金': 0, '现金': 0},
        'duration_days': 15,
        'probability': 0.08,
        'historical_reference': '2024年量化监管收紧, 微盘股暴跌',
        'early_signals': ['证监会量化监管吹风', '券商暂停DMA业务', '量化私募限规模'],
    },
    '国家队退场': {
        'category': '中国本土',
        'trigger': '汇金/证金宣布停止增持+开始减持, 市场失去托底力量',
        'impact': {'沪深300': -0.12, '有色': -0.15, '电力': -0.05, '电池': -0.12, '黄金': +0.02, '现金': 0},
        'duration_days': 20,
        'probability': 0.03,
        'historical_reference': '2015年救市资金退出讨论引发二次探底',
        'early_signals': ['汇金公告措辞变化', '救市ETF大额赎回', '国家队持仓披露'],
    },
    '人民币汇率崩盘': {
        'category': '中国本土',
        'trigger': '人民币单日贬超2%, 突破7.5关口, 资本外流加速',
        'impact': {'沪深300': -0.10, '有色': -0.08, '电力': -0.05, '电池': -0.10, '黄金': +0.10, '现金': 0},
        'duration_days': 20,
        'probability': 0.04,
        'historical_reference': '2015年811汇改, 人民币3天贬3%',
        'early_signals': ['CNH-CNY价差扩大', '外汇储备下降', '离岸人民币NDF贬值'],
    },
    '大规模退市潮': {
        'category': '中国本土',
        'trigger': '注册制下退市标准严格执行, 一次性退市50+家, 小盘股恐慌',
        'impact': {'沪深300': -0.05, '有色': -0.08, '电力': -0.02, '电池': -0.06, '黄金': 0, '现金': 0},
        'duration_days': 15,
        'probability': 0.06,
        'historical_reference': '2024年ST股批量退市',
        'early_signals': ['ST/*ST数量激增', '退市整理期股票增多', '证监会退市新规出台'],
    },

    # === 行业/技术 (4个) ===
    '锂电池技术路线颠覆': {
        'category': '行业技术',
        'trigger': '固态/钠电池量产成本低于锂电池, 磷酸铁锂需求崩塌',
        'impact': {'沪深300': -0.03, '有色': -0.05, '电力': 0, '电池': -0.30, '黄金': 0, '现金': 0},
        'duration_days': 90,
        'probability': 0.03,
        'historical_reference': '光伏PERC→TOPCon技术迭代, 旧产能减值',
        'early_signals': ['宁德/比亚迪技术路线公告', '钠电池量产新闻', '锂价持续下跌'],
    },
    '电力市场化改革失败': {
        'category': '行业技术',
        'trigger': '电价改革方案被否, 电力股估值逻辑崩塌',
        'impact': {'沪深300': -0.03, '有色': -0.02, '电力': -0.15, '电池': -0.02, '黄金': 0, '现金': 0},
        'duration_days': 30,
        'probability': 0.02,
        'historical_reference': '无直接先例',
        'early_signals': ['发改委电价政策变化', '电力股集体破位', '火电企业亏损扩大'],
    },
    'AI泡沫破裂': {
        'category': '行业技术',
        'trigger': '类似2000年互联网泡沫, AI公司大面积亏损, 估值崩塌',
        'impact': {'沪深300': -0.10, '有色': -0.12, '电力': -0.05, '电池': -0.15, '黄金': +0.05, '现金': 0},
        'duration_days': 60,
        'probability': 0.06,
        'historical_reference': '2000年纳斯达克泡沫破裂, 纳指跌78%',
        'early_signals': ['AI公司IPO破发', 'GPU订单下滑', '科技股估值回归'],
    },
    '碳交易价暴跌': {
        'category': '行业技术',
        'trigger': '碳配额过剩, 碳价从80元跌至30元, 新能源估值受损',
        'impact': {'沪深300': -0.02, '有色': -0.03, '电力': -0.08, '电池': -0.05, '黄金': 0, '现金': 0},
        'duration_days': 30,
        'probability': 0.05,
        'historical_reference': '欧洲碳价2023年从100跌至55欧元',
        'early_signals': ['碳配额拍卖流拍', '碳排放数据修订', 'EU-ETS碳价下跌'],
    },

    # === 组合冲击 (3个) ===
    '股债汇三杀': {
        'category': '组合冲击',
        'trigger': '股市暴跌+债市大跌+汇率贬值同时发生, 全面恐慌',
        'impact': {'沪深300': -0.25, '有色': -0.30, '电力': -0.15, '电池': -0.28, '黄金': +0.08, '现金': 0},
        'duration_days': 35,
        'probability': 0.02,
        'historical_reference': '2015年股灾+811汇改, 2022年英国养老金危机',
        'early_signals': ['股债同跌', '人民币快速贬值', '央行紧急会议'],
    },
    '两融爆仓潮': {
        'category': '组合冲击',
        'trigger': '融资余额2万亿→跌破平仓线→强平→踩踏→更多爆仓',
        'impact': {'沪深300': -0.18, '有色': -0.22, '电力': -0.08, '电池': -0.20, '黄金': +0.03, '现金': 0},
        'duration_days': 15,
        'probability': 0.04,
        'historical_reference': '2015年股灾杠杆踩踏',
        'early_signals': ['两融余额快速下降', '维持担保比例<130%', '券商强制平仓公告'],
    },
    '跨境券商清退冲击': {
        'category': '组合冲击',
        'trigger': '八部门整治→中概被迫清仓→A股联动暴跌→资金回流不及预期',
        'impact': {'沪深300': -0.08, '有色': -0.12, '电力': -0.03, '电池': -0.10, '黄金': +0.02, '现金': 0},
        'duration_days': 20,
        'probability': 0.07,
        'historical_reference': '2026年5月22日 八部门整治→老虎跌38%, A股4200点天量跳水',
        'early_signals': ['中概持续暴跌', '老虎/富途成交量萎缩', '外汇局资本管制收紧'],
    },
}


class BlackSwanEngine:
    """
    黑天鹅压力测试引擎

    用法:
        bse = BlackSwanEngine(portfolio={'沪深300': 148, '有色': 94, '电力': 168, '电池': 80, '现金': 0})
        report = bse.stress_test_all()
        plan = bse.contingency_plan('中美金融脱钩升级')
    """

    def __init__(self, portfolio=None):
        """
        Args:
            portfolio: {板块名: 金额} 或 持仓列表
        """
        self.portfolio = portfolio or {}
        self.scenarios = BLACK_SWAN_SCENARIOS
        self.results = {}

    def set_portfolio(self, holdings):
        """从持仓列表设置portfolio"""
        sector_map = {
            '有色金属': '有色', '电力': '电力', '沪深300': '沪深300',
            '锂电池': '电池', '新能源车': '电池',
        }
        pf = {'现金': 0}
        for h in holdings:
            sector = sector_map.get(h.get('sector', ''), '沪深300')
            pf[sector] = pf.get(sector, 0) + h.get('amount', 0)
        self.portfolio = pf

    def stress_test_single(self, scenario_name):
        """单个黑天鹅压力测试"""
        if scenario_name not in self.scenarios:
            return {'error': f'场景不存在: {scenario_name}'}

        sc = self.scenarios[scenario_name]
        total_loss = 0
        impact_detail = {}

        for asset, amount in self.portfolio.items():
            impact_rate = sc['impact'].get(asset, sc['impact'].get('沪深300', -0.10))
            loss = amount * impact_rate
            total_loss += loss
            impact_detail[asset] = {
                'amount': amount,
                'impact_rate': impact_rate,
                'loss': round(loss, 2),
                'remaining': round(amount + loss, 2),
            }

        total_amount = sum(self.portfolio.values())
        total_remaining = total_amount + total_loss

        return {
            'scenario': scenario_name,
            'category': sc['category'],
            'trigger': sc['trigger'],
            'probability': sc['probability'],
            'duration_days': sc['duration_days'],
            'total_before': round(total_amount, 2),
            'total_loss': round(abs(total_loss), 2),
            'total_loss_pct': round(abs(total_loss) / total_amount * 100, 1) if total_amount > 0 else 0,
            'total_remaining': round(total_remaining, 2),
            'impact_detail': impact_detail,
            'early_signals': sc['early_signals'],
            'contingency': self._generate_contingency(sc, total_loss, total_amount),
        }

    def _generate_contingency(self, scenario, total_loss, total_amount):
        """自动生成应对预案"""
        loss_pct = abs(total_loss) / total_amount if total_amount > 0 else 0

        actions = []
        # 三级响应
        if loss_pct > 0.25:
            actions.append('🔴 一级响应: 全仓清仓, 转现金/货币基金')
            actions.append('🔴 暂停所有新开仓, 等待信号明确')
            actions.append('🔴 如有黄金/国债ETF, 保留作为对冲')
        elif loss_pct > 0.15:
            actions.append('🟠 二级响应: 减仓至30%以下, 保留强势品种')
            actions.append('🟠 防御品种(电力/公用事业)可保留, 周期品种(有色/新能源)清仓')
            actions.append('🟠 每天复盘, 关注早期信号是否改善')
        elif loss_pct > 0.05:
            actions.append('🟡 三级响应: 减仓至50%, 收紧止损至3%')
            actions.append('🟡 暂停加仓, 持有现金等方向')
        else:
            actions.append('🟢 常规应对: 按正常止损规则执行')
            actions.append('🟢 关注早期信号, 做好升级准备')

        # 对冲建议
        hedges = []
        for asset, impact in scenario['impact'].items():
            if impact > 0.05 and asset not in self.portfolio:
                hedges.append(f'{asset}(+{impact:.0%})')

        return {
            'response_level': '一级' if loss_pct > 0.25 else ('二级' if loss_pct > 0.15 else ('三级' if loss_pct > 0.05 else '常规')),
            'actions': actions,
            'hedge_suggestions': hedges if hedges else ['现金为王, 不对冲'],
            'max_acceptable_loss': round(total_amount * 0.10, 2),  # 铁律: 最大10%回撤
            'exceeds_max_loss': loss_pct > 0.10,
        }

    def stress_test_all(self):
        """全场景压力测试"""
        self.results = {}
        for name in self.scenarios:
            self.results[name] = self.stress_test_single(name)
        return self.results

    def contingency_plan(self, scenario_name):
        """获取特定场景的应对预案"""
        return self.stress_test_single(scenario_name)

    def worst_case_report(self, top_n=5):
        """最坏N个场景报告"""
        if not self.results:
            self.stress_test_all()

        sorted_results = sorted(
            self.results.values(),
            key=lambda x: x['total_loss'],
            reverse=True
        )

        lines = ['=' * 70, '  黑天鹅压力测试 · 最坏场景TOP{}'.format(top_n), '=' * 70]
        total = sum(self.portfolio.values())
        lines.append(f'  组合总额: {total:.0f}元 | 场景库: {len(self.scenarios)}个')
        lines.append('-' * 70)

        for i, r in enumerate(sorted_results[:top_n]):
            lines.append(
                f'  #{i+1} [{r["category"]}] {r["scenario"]}: '
                f'亏损{r["total_loss"]:.0f}元({r["total_loss_pct"]:.1f}%) | '
                f'概率{r["probability"]:.0%} | {r["duration_days"]}天'
            )
            lines.append(f'      触发: {r["trigger"][:60]}')
            lines.append(f'      响应: {r["contingency"]["response_level"]}级 | '
                        f'超10%红线: {"⚠是" if r["contingency"]["exceeds_max_loss"] else "✅否"}')
            for act in r['contingency']['actions'][:2]:
                lines.append(f'      {act}')

        # 统计超红线场景
        over_limit = sum(1 for r in self.results.values() if r['contingency']['exceeds_max_loss'])
        lines.append('-' * 70)
        lines.append(f'  {over_limit}/{len(self.scenarios)}个场景超10%最大回撤红线')
        if over_limit > 0:
            lines.append('  ⚠ 建议: 减小仓位或增加对冲, 使超红线场景数降至0')
        lines.append('=' * 70)

        return '\n'.join(lines)

    def early_warning_scan(self, market_data):
        """
        早期信号扫描: 检查当前市场是否触发任何黑天鹅的早期信号

        Args:
            market_data: {indicator: value, ...}

        Returns:
            [{scenario, matched_signals, probability_upgrade}]
        """
        warnings = []
        for name, sc in self.scenarios.items():
            matched = []
            for signal in sc['early_signals']:
                # 简化匹配: 检查market_data中是否有相关指标
                for key, val in market_data.items():
                    if key in signal or signal.split('>')[0].strip() in key:
                        matched.append(signal)
                        break
            if matched:
                # 概率升级: 每匹配一个信号, 概率×2
                upgraded_prob = min(0.50, sc['probability'] * (2 ** len(matched)))
                warnings.append({
                    'scenario': name,
                    'base_probability': sc['probability'],
                    'upgraded_probability': round(upgraded_prob, 4),
                    'matched_signals': matched,
                    'category': sc['category'],
                })

        warnings.sort(key=lambda x: x['upgraded_probability'], reverse=True)
        return warnings

    def report(self):
        """完整压力测试报告"""
        worst = self.worst_case_report(5)
        warnings = self.early_warning_scan({})

        lines = [worst]
        if warnings:
            lines.append('')
            lines.append('  早期信号监控:')
            for w in warnings[:5]:
                lines.append(f'    [{w["category"]}] {w["scenario"]}: '
                           f'概率{w["base_probability"]:.0%}→{w["upgraded_probability"]:.0%} '
                           f'(匹配{w["matched_signals"]})')
        return '\n'.join(lines)


# ===== 便捷函数 =====

def quick_stress_test(holdings):
    """快速压力测试: 传入持仓列表→返回最坏5场景"""
    bse = BlackSwanEngine()
    bse.set_portfolio(holdings)
    bse.stress_test_all()
    return bse.worst_case_report(5)


if __name__ == '__main__':
    # 用当前portfolio测试
    portfolio = {'沪深300': 148, '有色': 94, '电力': 168, '电池': 80, '现金': 0}
    bse = BlackSwanEngine(portfolio)
    bse.stress_test_all()

    print(bse.worst_case_report(5))

    print('\n=== 特定场景预案: 中美金融脱钩 ===')
    plan = bse.contingency_plan('中美金融脱钩升级')
    for k, v in plan.items():
        if k != 'impact_detail':
            print(f'  {k}: {v}')

    print('\n=== 预售警信号扫描 ===')
    test_data = {'美10Y': 4.57, 'VIX': 22, '北向资金连续3日净流出>100亿': True}
    warnings = bse.early_warning_scan(test_data)
    for w in warnings:
        print(f'  {w["scenario"]}: {w["base_probability"]:.0%}→{w["upgraded_probability"]:.0%}')
