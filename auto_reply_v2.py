# -*- coding: utf-8 -*-
"""
抖音多账号私信自动回复 v2
==========================
参考同事已验证方案：Selenium + Chrome Profile + DOM注入
每个账号独立 Chrome 用户目录，互不干扰
"""

import os
import sys
import re
import json
import time
import threading
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QSplitter, QMessageBox, QFileDialog, QGroupBox, QCheckBox,
    QStatusBar
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QColor

# ====== 配置 ======

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(BASE_DIR, "chrome_profiles")
CONFIG_FILE = os.path.join(BASE_DIR, "rules.json")
LOG_FILE = os.path.join(BASE_DIR, "reply_log.csv")

os.makedirs(PROFILES_DIR, exist_ok=True)


def load_rules():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "accounts": [
            {
                "name": "账号1",
                "enabled": True,
                "rules": [
                    {"keyword": "在吗", "reply": "在的，有什么可以帮您？"},
                    {"keyword": "利率", "reply": "您好！具体利率请拨打96688咨询~"},
                    {"keyword": "贷款", "reply": "您好！贷款请到网点或拨打96688咨询~"},
                ],
                "phone_reply": "好的，已收到您的手机号，稍后联系您~",
                "default_reply": "您好！感谢关注遵义农商银行，请拨打96688或到网点咨询~",
                "poll_interval": 5,
            }
        ]
    }


def save_rules(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ====== Chrome 管理 ======

def find_chrome():
    if sys.platform == "win32":
        for p in [
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
            os.path.expandvars("%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"),
        ]:
            if os.path.exists(p):
                return p
    return None


def get_driver_path():
    exe = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
    local = os.path.join(BASE_DIR, exe)
    if os.path.exists(local):
        return local
    import shutil
    d = shutil.which(exe)
    return d


# ====== 工作线程 ======

class AccountWorker(QThread):
    log_signal = pyqtSignal(str, str)
    status_signal = pyqtSignal(str, str)
    reply_signal = pyqtSignal(str, str, str)

    def __init__(self, account_config, account_index):
        super().__init__()
        self.config = account_config
        self.idx = account_index
        self.name = account_config["name"]
        self.rules = account_config.get("rules", [])
        self.phone_reply = account_config.get("phone_reply", "")
        self.default_reply = account_config.get("default_reply", "")
        self.poll = account_config.get("poll_interval", 5)
        self._stop = False
        self._login_ok = threading.Event()
        self._replied = set()

    def confirm_login(self):
        self._login_ok.set()

    def log(self, msg):
        self.log_signal.emit(self.name, msg)

    def stop(self):
        self._stop = True

    def _scan_reds(self, driver):
        """扫描红点——多种选择器兜底，跳过群聊"""
        raw = driver.execute_script("""
            let reds = [];
            let debug = {total: 0, withBadge: 0};
            try {
                // 查找私信列表项：尝试多种可能的class名
                let items = document.querySelectorAll(
                    '[class*="conversation"], [class*="session"], [class*="chat-item"], ' +
                    '[class*="contact-item"], [class*="list-item"], [class*="message-item"], ' +
                    'div[class*="Cov"], [class*="user-item"], li[class*="item"]'
                );
                // 如果什么都没找到，说明抖音用了完全不同的结构，尝试所有div
                if (items.length < 2) {
                    items = document.querySelectorAll('div[class]');
                }
                debug.total = items.length;

                for (let i = 0; i < items.length; i++) {
                    let el = items[i];
                    let text = (el.textContent || '').trim();
                    if (!text || text.length > 300) continue;
                    if (text.includes('群聊')) continue;

                    // 找红点：sup标签、带数字的span/div、红色背景的小元素
                    let badge = el.querySelector(
                        'sup, [class*="badge"], [class*="unread"], [class*="count"], ' +
                        '[class*="num"], [class*="red"], [class*="dot"]'
                    );
                    if (badge) {
                        let t = badge.textContent.trim();
                        if (t && (/\\d/.test(t) || t === 'new' || t === '新')) {
                            debug.withBadge++;
                            let cid = el.getAttribute('data-id') || el.getAttribute('data-key') || ('cid_'+i);
                            let name = '';
                            let sp = el.querySelector('span, [class*="name"], [class*="nick"]');
                            if (sp) name = sp.textContent.trim().substring(0,15);
                            reds.push({id: cid, index: i, name: name || text.substring(0,12), unread: t});
                        }
                    }
                }
            } catch(e) {}
            reds._debug = debug;
            // 把debug信息也放进JSON（JSON.stringify不序列化自定义属性）
            reds._debug = debug;
            let out = JSON.parse(JSON.stringify(reds));
            out._debug = debug;
            return JSON.stringify(out);
        """)
        result = json.loads(raw) if raw else []
        # 每20次循环输出一次调试信息（避免刷屏）
        if not hasattr(self, '_scan_count'): self._scan_count = 0
        self._scan_count += 1
        if self._scan_count % 4 == 1:
            dbg = result.get('_debug', {}) if isinstance(result, dict) else {}
            cleaned = [r for r in (result if isinstance(result, list) else []) if isinstance(r, dict) and 'id' in r]
            if not cleaned:
                self.log(f"扫描: 页面元素{dbg.get('total','?')}个, 未发现红点")
            return cleaned
        return [r for r in (result if isinstance(result, list) else []) if isinstance(r, dict) and 'id' in r]

    def _click_red_item(self, driver, name):
        """点击红点对话——找带数字的 sup/span，点击其父元素"""
        clicked = driver.execute_script("""
            // 找到所有 sup 标签（通常是红点数字）
            let badges = document.querySelectorAll('sup, span[class*="count"], span[class*="num"]');
            for (let b of badges) {
                let t = b.textContent.trim();
                if (t && /\\d/.test(t)) {
                    // 向上找到可点击的父级（对话列表项）
                    let item = b;
                    for (let depth = 0; depth < 5 && item; depth++) {
                        if (item.tagName === 'DIV' && item.className) {
                            let text = item.textContent || '';
                            if (text.length > 2 && text.length < 200 && !text.includes('群聊')) {
                                item.click();
                                return JSON.stringify({ok: true, name: text.substring(0,20)});
                            }
                        }
                        item = item.parentElement;
                    }
                }
            }
            return JSON.stringify({ok: false});
        """)
        result = json.loads(clicked) if clicked else {"ok": False}
        return result.get("ok", False)

    def _back_to_list(self, driver):
        """退回消息列表——多种方式尝试"""
        driver.execute_script("""
            // 方法1：找返回箭头
            let back = document.querySelector('[class*="back"], [class*="return"], [class*="arrow"], svg');
            if (back) { back.closest('div,button,span').click(); return; }
            // 方法2：点消息tab
            let tabs = document.querySelectorAll('[class*="tab"], [class*="nav"] span, [class*="nav"] div');
            for (let t of tabs) {
                if (/消息/.test(t.textContent)) { t.click(); return; }
            }
            // 方法3：模拟ESC或点击遮罩
            let overlay = document.querySelector('[class*="overlay"], [class*="mask"]');
            if (overlay) { overlay.click(); return; }
            // 方法4：通过URL导航（最后手段）
            if (!window.location.href.includes('/messages')) {
                window.location.href = 'https://www.douyin.com/messages';
            }
        """)

    def _send_reply(self, driver, text):
        """定位输入框 → 键盘逐字输入 → Enter 发送 → 返回是否成功"""
        found = driver.execute_script("""
            let inp = null;
            let all = document.querySelectorAll('div[contenteditable="true"], textarea');
            for (let el of all) {
                let r = el.getBoundingClientRect();
                if (r.height > 20 && r.height < 200 && r.top > window.innerHeight * 0.35) {
                    inp = el; break;
                }
            }
            if (!inp) {
                inp = document.querySelector('div[data-placeholder]') || document.querySelector('div[class*="rich-input"]');
            }
            if (inp) { inp.focus(); inp.click(); }
            return !!inp;
        """)
        if not found:
            return False
        time.sleep(0.3)
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
        actions = ActionChains(driver)
        for ch in text:
            actions.send_keys(ch)
        actions.pause(0.3)
        actions.send_keys(Keys.ENTER)
        actions.perform()
        return True

    def _write_log(self, sender, msg_in, msg_out):
        """写入CSV日志"""
        try:
            import csv
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            existed = os.path.exists(LOG_FILE)
            with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if not existed:
                    w.writerow(["时间", "账号", "联系人", "收到消息", "回复内容"])
                w.writerow([ts, self.name, sender, msg_in, msg_out])
        except:
            pass

    def match_reply(self, text):
        """规则匹配：手机号 > 关键词 > 默认"""
        if self.phone_reply and re.search(r'1[3-9]\d{9}', text):
            return self.phone_reply, "手机号规则"
        for rule in self.rules:
            kw = rule.get("keyword", "")
            if kw and kw in text:
                return rule["reply"], f"关键词: {kw}"
        return self.default_reply, "默认规则"

    def run(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.common.by import By
        except ImportError:
            self.log("缺少 selenium 库")
            self.status_signal.emit(self.name, "错误")
            return

        chrome = find_chrome()
        if not chrome:
            self.log("未找到 Chrome 浏览器")
            self.status_signal.emit(self.name, "错误")
            return

        profile = os.path.join(PROFILES_DIR, f"account_{self.idx}")
        os.makedirs(profile, exist_ok=True)

        opts = Options()
        opts.binary_location = chrome
        opts.add_argument(f"--user-data-dir={profile}")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("detach", True)

        drv_path = get_driver_path()
        svc = Service(executable_path=drv_path) if drv_path else Service()

        try:
            driver = webdriver.Chrome(service=svc, options=opts)
        except Exception as e:
            self.log(f"Chrome 启动失败: {e}")
            self.status_signal.emit(self.name, "错误")
            return

        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        driver.set_window_size(500, 800)

        try:
            driver.get("https://www.douyin.com")
            self.log("Chrome 已打开，请扫码登录，然后手动进入「消息」页面")
            self.status_signal.emit(self.name, "等待登录")

            # 等待用户点击「确认已登录」
            self.log("完成后请点击软件上的「确认已登录」按钮")
            while not self._login_ok.is_set() and not self._stop:
                time.sleep(0.5)

            if self._stop:
                return

            self.status_signal.emit(self.name, "监控中")
            self.log("✅ 开始监控私信")

            # 用 (conversation_id, unread_count) 追踪：只有count变大才说明新消息
            seen_counts = {}  # cid -> last_unread_count

            while not self._stop:
                try:
                    reds = self._scan_reds(driver)
                    
                    for red in reds:
                        if self._stop: break
                        cid = red.get("id")
                        count = int(red.get("unread", "1").replace("+","")) if red.get("unread","1").replace("+","").isdigit() else 1
                        
                        if cid in seen_counts and count <= seen_counts[cid]:
                            continue

                        name = red.get("name", "用户")

                        # === 步骤1：点击红点对话 ===
                        self.log(f"点击红点: {name}({count}条)")
                        clicked = self._click_red_item(driver, name)
                        if not clicked:
                            continue
                        time.sleep(2)

                        # === 步骤2：确认红点消失 ===
                        badge_gone = driver.execute_script("""
                            let badges = document.querySelectorAll('sup, [class*="badge"], [class*="unread"]');
                            for (let b of badges) {
                                let t = b.textContent.trim();
                                if (t && /\\d/.test(t)) return false;
                            }
                            // 也检查一下是否进入了聊天画面
                            let input = document.querySelector('div[contenteditable="true"], textarea');
                            return !!input;
                        """)
                        if not badge_gone:
                            self.log(f"红点未消失或未进入对话，跳过")
                            self._back_to_list(driver)
                            continue
                        time.sleep(1)

                        # === 步骤3：读消息 + 回复 ===
                        last_msg = driver.execute_script("""
                            let all = [];
                            let sels = ['div[class*="message-content"]', 'div[class*="bubble"]',
                                'div[class*="chat-msg"]', 'div[class*="text-item"]',
                                'span[class*="content"]', 'div[class*="im-message"]'];
                            for (let s of sels) {
                                let found = document.querySelectorAll(s);
                                if (found.length > 0) { all = found; break; }
                            }
                            for (let i = all.length - 1; i >= 0; i--) {
                                let t = all[i].textContent.trim();
                                if (t.length > 0 && t.length < 500 && !t.includes('发送') && !t.includes('输入'))
                                    return t;
                            }
                            return '';
                        """)

                        reply_text, rule_name = self.match_reply(last_msg)
                        if reply_text:
                            ok = self._send_reply(driver, reply_text)
                            if ok:
                                self.log(f"📤 回复{name}: {reply_text[:30]} [{rule_name}]")
                                self._write_log(name, last_msg, reply_text)
                                seen_counts[cid] = count
                                time.sleep(1)

                        # === 步骤4：退回列表 ===
                        self._back_to_list(driver)
                        time.sleep(2)

                    # 如果连续多轮没找到红点，重置所有计数（可能页面刷新了）
                    if not reds:
                        if not hasattr(self, '_empty_rounds'): self._empty_rounds = 0
                        self._empty_rounds += 1
                        if self._empty_rounds > 10:
                            seen_counts.clear()
                            self._empty_rounds = 0
                    else:
                        self._empty_rounds = 0

                    time.sleep(self.poll)

                except Exception as e:
                    self.log(f"检测异常: {str(e)[:80]}")
                    time.sleep(self.poll)

        except Exception as e:
            self.log(f"运行异常: {e}")
        finally:
            try:
                driver.quit()
            except:
                pass
            self.log("已停止")
            self.status_signal.emit(self.name, "已停止")


# ====== GUI ======

STYLE = """
QMainWindow { background-color: #1e1e1e; }
QTabWidget::pane { border: 1px solid #333; background: #252525; }
QTabBar::tab { background: #2d2d2d; color: #aaa; padding: 8px 16px; border: none; }
QTabBar::tab:selected { background: #c41230; color: white; }
QTableWidget { background: #1a1a1a; color: #ddd; gridline-color: #333; border: 1px solid #333; }
QHeaderView::section { background: #2d2d2d; color: #aaa; border: none; padding: 4px; }
QLineEdit, QTextEdit { background: #2d2d2d; color: #ddd; border: 1px solid #444; padding: 4px; border-radius: 4px; }
QPushButton { background: #3a3a3a; color: #ddd; border: none; padding: 6px 14px; border-radius: 4px; }
QPushButton:hover { background: #4a4a4a; }
QPushButton#btnStart { background: #c41230; color: white; font-weight: bold; }
QPushButton#btnStart:hover { background: #e01438; }
QPushButton#btnStop { background: #555; color: white; }
QPushButton#btnAdd { background: #25f4ee; color: #000; }
QLabel { color: #bbb; }
QGroupBox { color: #aaa; border: 1px solid #333; border-radius: 6px; margin-top: 8px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; }
QStatusBar { background: #2d2d2d; color: #aaa; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("抖音多账号私信自动回复 - 遵义农商银行")
        self.setGeometry(100, 100, 1000, 700)
        self.setStyleSheet(STYLE)

        self.config = load_rules()
        self.workers = {}
        self.tabs = {}

        self._build_ui()
        self._load_tabs()
        self.statusBar().showMessage("就绪 — 添加账号并配置回复规则后启动")
        self.statusBar().setStyleSheet("background:#2d2d2d;color:#aaa;")

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # 顶部按钮
        top = QHBoxLayout()
        btn_add_tab = QPushButton("+ 添加账号")
        btn_add_tab.setObjectName("btnAdd")
        btn_add_tab.clicked.connect(self._add_account)
        top.addWidget(btn_add_tab)

        btn_save = QPushButton("💾 保存规则")
        btn_save.clicked.connect(self._save)
        top.addWidget(btn_save)

        top.addStretch()

        self.btn_start_all = QPushButton("▶ 全部启动")
        self.btn_start_all.setObjectName("btnStart")
        self.btn_start_all.clicked.connect(self._start_all)
        top.addWidget(self.btn_start_all)

        self.btn_stop_all = QPushButton("⏹ 全部停止")
        self.btn_stop_all.setObjectName("btnStop")
        self.btn_stop_all.clicked.connect(self._stop_all)
        top.addWidget(self.btn_stop_all)

        main_layout.addLayout(top)

        # Tab 区域
        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        # 日志区域
        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        log_btns = QHBoxLayout()
        b = QPushButton("导出CSV"); b.clicked.connect(self._export_log); log_btns.addWidget(b)
        log_btns.addStretch(); log_layout.addLayout(log_btns)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        self.log_text.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_text)
        main_layout.addWidget(log_group)

    def _load_tabs(self):
        self.tab_widget.clear()
        self.tabs.clear()
        for i, acc in enumerate(self.config.get("accounts", [])):
            self._add_tab(i, acc)

    def _add_tab(self, i, acc):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # 账号基本信息
        info_row = QHBoxLayout()
        info_row.addWidget(QLabel("账号名称:"))
        name_input = QLineEdit(acc.get("name", f"账号{i+1}"))
        info_row.addWidget(name_input)

        enabled_cb = QCheckBox("启用")
        enabled_cb.setChecked(acc.get("enabled", True))
        info_row.addWidget(enabled_cb)
        info_row.addStretch()

        status_label = QLabel("⚪ 未启动")
        info_row.addWidget(status_label)
        layout.addLayout(info_row)

        # 规则表格
        rules_table = QTableWidget()
        rules_table.setColumnCount(3)
        rules_table.setHorizontalHeaderLabels(["关键词", "回复内容", ""])
        rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        rules_table.setColumnWidth(0, 120)
        rules_table.setColumnWidth(2, 40)

        rules = acc.get("rules", [])
        rules_table.setRowCount(max(len(rules), 1))
        for j, rule in enumerate(rules):
            rules_table.setItem(j, 0, QTableWidgetItem(rule.get("keyword", "")))
            rules_table.setItem(j, 1, QTableWidgetItem(rule.get("reply", "")))

        layout.addWidget(QLabel("关键词回复规则:"))
        layout.addWidget(rules_table)

        # 规则操作按钮
        rule_btns = QHBoxLayout()
        btn_add_rule = QPushButton("+ 添加规则")
        btn_add_rule.clicked.connect(lambda: self._add_rule_row(rules_table))
        rule_btns.addWidget(btn_add_rule)

        btn_del_rule = QPushButton("- 删除选中")
        btn_del_rule.clicked.connect(lambda: self._del_rule_row(rules_table))
        rule_btns.addWidget(btn_del_rule)
        rule_btns.addStretch()
        layout.addLayout(rule_btns)

        # 手机号和默认回复
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(QLabel("手机号回复:"))
        phone_input = QLineEdit(acc.get("phone_reply", "好的，已收到您的手机号，稍后联系您~"))
        bottom_row.addWidget(phone_input)
        layout.addLayout(bottom_row)

        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("默认回复:"))
        default_input = QLineEdit(acc.get("default_reply", "您好！感谢关注遵义农商银行~"))
        default_row.addWidget(default_input)
        layout.addLayout(default_row)

        # 轮询间隔
        poll_row = QHBoxLayout()
        poll_row.addWidget(QLabel("检测间隔(秒):"))
        poll_input = QLineEdit(str(acc.get("poll_interval", 5)))
        poll_input.setMaximumWidth(60)
        poll_row.addWidget(poll_input)
        poll_row.addStretch()

        # 启动/停止/确认按钮
        btn_start = QPushButton("▶ 启动")
        btn_start.setObjectName("btnStart")
        btn_start.clicked.connect(lambda _, idx=i: self._start_one(idx))
        poll_row.addWidget(btn_start)

        btn_confirm = QPushButton("✓ 确认已登录")
        btn_confirm.setStyleSheet("background:#25f4ee;color:#000;font-weight:bold;")
        btn_confirm.clicked.connect(lambda _, idx=i: self._confirm_login(idx))
        poll_row.addWidget(btn_confirm)

        btn_stop = QPushButton("⏹ 停止")
        btn_stop.clicked.connect(lambda _, idx=i: self._stop_one(idx))
        poll_row.addWidget(btn_stop)

        layout.addLayout(poll_row)

        # 删除账号
        del_row = QHBoxLayout()
        del_row.addStretch()
        btn_del = QPushButton("🗑 删除账号")
        btn_del.clicked.connect(lambda _, idx=i: self._del_account(idx))
        del_row.addWidget(btn_del)
        layout.addLayout(del_row)

        self.tab_widget.addTab(tab, acc.get("name", f"账号{i+1}"))

        self.tabs[i] = {
            "name": name_input,
            "enabled": enabled_cb,
            "status": status_label,
            "rules_table": rules_table,
            "phone": phone_input,
            "default": default_input,
            "poll": poll_input,
        }

    def _add_account(self):
        n = len(self.config["accounts"]) + 1
        self.config["accounts"].append({
            "name": f"账号{n}",
            "enabled": True,
            "rules": [{"keyword": "在吗", "reply": "在的，有什么可以帮您？"}],
            "phone_reply": "好的，已收到您的手机号，稍后联系您~",
            "default_reply": "您好！感谢关注遵义农商银行，请拨打96688或到网点咨询~",
            "poll_interval": 5,
        })
        self._load_tabs()
        self._log("系统", f"已添加账号{n}")

    def _del_account(self, idx):
        reply = QMessageBox.question(self, "确认", f"删除 {self.config['accounts'][idx]['name']}？")
        if reply == QMessageBox.Yes:
            self._stop_one(idx)
            del self.config["accounts"][idx]
            save_rules(self.config)
            self._load_tabs()

    def _add_rule_row(self, table):
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(""))
        table.setItem(row, 1, QTableWidgetItem(""))

    def _del_rule_row(self, table):
        row = table.currentRow()
        if row >= 0 and table.rowCount() > 1:
            table.removeRow(row)

    def _read_tab(self, idx):
        t = self.tabs[idx]
        table = t["rules_table"]
        rules = []
        for r in range(table.rowCount()):
            kw = table.item(r, 0)
            rp = table.item(r, 1)
            if kw and kw.text().strip():
                rules.append({"keyword": kw.text().strip(), "reply": (rp.text() if rp else "")})
        return {
            "name": t["name"].text(),
            "enabled": t["enabled"].isChecked(),
            "rules": rules,
            "phone_reply": t["phone"].text(),
            "default_reply": t["default"].text(),
            "poll_interval": int(t["poll"].text()) if t["poll"].text().isdigit() else 5,
        }

    def _save(self):
        for i in range(len(self.config["accounts"])):
            self.config["accounts"][i] = self._read_tab(i)
        save_rules(self.config)
        self._log("系统", "规则已保存")

    def _start_one(self, idx):
        self._save()
        acc = self.config["accounts"][idx]
        if not acc["enabled"]:
            return

        if acc["name"] in self.workers:
            self._log("系统", f"{acc['name']} 已在运行")
            return

        worker = AccountWorker(acc, idx)
        worker.log_signal.connect(self._log)
        worker.status_signal.connect(lambda n, s, i=idx: self._update_status(i, s))
        worker.reply_signal.connect(self._record_reply)
        worker.start()

        self.workers[acc["name"]] = worker
        self.tabs[idx]["status"].setText("🟡 等待登录...")

    def _confirm_login(self, idx):
        nm = self.config["accounts"][idx]["name"]
        if nm in self.workers:
            self.workers[nm].confirm_login()
            self.tabs[idx]["status"].setText("🟢 监控中")
            self.tabs[idx]["status"].setStyleSheet("color:#25f4ee;")
            self._log("系统", f"用户确认已登录，开始监控")

    def _stop_one(self, idx):
        acc_name = self.config["accounts"][idx]["name"]
        if acc_name in self.workers:
            self.workers[acc_name].stop()
            self.workers[acc_name].wait(5000)
            del self.workers[acc_name]
        self.tabs[idx]["status"].setText("⚪ 未启动")

    def _start_all(self):
        self._save()
        for i in range(len(self.config["accounts"])):
            if self.config["accounts"][i]["enabled"]:
                self._start_one(i)

    def _stop_all(self):
        for i in range(len(self.config["accounts"])):
            self._stop_one(i)

    def _update_status(self, idx, status):
        colors = {
            "监控中": "#25f4ee", "等待登录": "#ff9a44",
            "已停止": "#aaa", "错误": "#fe2c55"
        }
        color = colors.get(status, "#aaa")
        self.tabs[idx]["status"].setText(f"● {status}")
        self.tabs[idx]["status"].setStyleSheet(f"color:{color};")

    def _log(self, name, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{ts}] [{name}] {msg}")

    def _record_reply(self, name, incoming, reply):
        self._log(name, f"收到: {incoming} → 回复: {reply}")

    def closeEvent(self, event):
        self._stop_all()
        self._save()
        event.accept()

    def _export_log(self):
        if not os.path.exists(LOG_FILE):
            QMessageBox.information(self, "提示", "暂无日志")
            return
        p, _ = QFileDialog.getSaveFileName(self, "导出日志", "回复记录.csv", "CSV(*.csv)")
        if p:
            import shutil; shutil.copy(LOG_FILE, p)
            self._log("系统", f"日志已导出: {p}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
