"""天眼五层验证塔 — 统一入口 (L0→L1→L2→L3→L4)
用法: python verification_tower.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conflict_resolver import get_full_verification_report

if __name__ == '__main__':
    get_full_verification_report()
