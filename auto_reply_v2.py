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
    log_signal = pyqtSignal(str, str)  # account_name, message
    status_signal = pyqtSignal(str, str)  # account_name, status
    reply_signal = pyqtSignal(str, str, str)  # account_name, incoming, reply

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

    def log(self, msg):
        self.log_signal.emit(self.name, msg)

    def stop(self):
        self._stop = True

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
            self.log("Chrome 已打开，请扫码登录")
            self.status_signal.emit(self.name, "等待登录")

            # 等待用户手动登录并点进消息页
            self.log("登录后请手动点进「消息」页面，程序自动检测新消息")
            time.sleep(8)  # 给用户时间操作

            self.status_signal.emit(self.name, "监控中")
            self.log("✅ 开始监控私信")

            replied_ids = set()

            while not self._stop:
                try:
                    # 检测未读消息
                    js_check = """
                    let results = [];
                    try {
                        let items = document.querySelectorAll('[class*="conversation"], [class*="session"], div[class*="Cov"]');
                        for (let item of items) {
                            let badge = item.querySelector('sup, [class*="badge"], [class*="unread"], [class*="count"]');
                            if (badge) {
                                let t = badge.textContent.trim();
                                if (t && t !== '0' && /\\d/.test(t)) {
                                    results.push({text: item.textContent.trim().substring(0, 100), unread: t});
                                }
                            }
                        }
                    } catch(e) {}
                    return JSON.stringify(results);
                    """
                    raw = driver.execute_script(js_check)
                    unread = json.loads(raw)

                    if unread:
                        for msg in unread:
                            msg_id = msg.get("text", "")[:50]
                            if msg_id in replied_ids:
                                continue
                            replied_ids.add(msg_id)

                            incoming_text = msg.get("text", "")
                            reply_text, rule_name = self.match_reply(incoming_text)

                            if reply_text:
                                # 点击第一个未读对话
                                driver.execute_script("""
                                    for (let item of document.querySelectorAll('[class*="conversation"], [class*="session"], div[class*="Cov"]')) {
                                        let b = item.querySelector('sup, [class*="badge"], [class*="unread"], [class*="count"]');
                                        if (b) { let t = b.textContent.trim(); if (t && t !== '0' && /\\d/.test(t)) { item.click(); break; } }
                                    }
                                """)
                                time.sleep(2)

                                # 读取最后一条消息
                                last_msg = driver.execute_script("""
                                    let msgs = document.querySelectorAll('[class*="message"], [class*="msg"], div[class*="bubble"]');
                                    return msgs.length > 0 ? msgs[msgs.length-1].textContent.trim() : '';
                                """)

                                # 发回复
                                driver.execute_script(f"""
                                    let reply = {json.dumps(reply_text, ensure_ascii=False)};
                                    let input = document.querySelector('textarea, [contenteditable="true"], div[contenteditable]');
                                    if (!input) input = document.querySelector('[class*="input"], [class*="editor"]');
                                    if (input) {{
                                        if (input.tagName === 'TEXTAREA') input.value = reply;
                                        else input.textContent = reply;
                                        input.dispatchEvent(new Event('input', {{bubbles: true}}));
                                        setTimeout(function() {{
                                            input.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', bubbles: true}}));
                                        }}, 500);
                                    }}
                                    JSON.stringify({{sent: !!input}});
                                """)

                                self.log(f"📤 [{rule_name}] → {reply_text[:30]}...")
                                self.reply_signal.emit(self.name, last_msg[:40], reply_text[:40])
                                time.sleep(2)

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

        # 启动/停止按钮
        btn_start = QPushButton("▶ 启动")
        btn_start.setObjectName("btnStart")
        btn_start.clicked.connect(lambda _, idx=i: self._start_one(idx))
        poll_row.addWidget(btn_start)

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


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
