# -*- coding: utf-8 -*-
"""
抖音多账号私信自动回复 v3
==========================
核心逻辑：
1. 保持在消息列表页面扫描红点
2. 回复后退回列表 → 继续扫描下一个红点
3. 已回复的对话不重复回复
4. 跳过群聊
5. 日志可导出
"""

import os, sys, re, json, time, csv, threading
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QFileDialog, QGroupBox, QCheckBox, QStatusBar
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(BASE_DIR, "chrome_profiles")
CONFIG_FILE = os.path.join(BASE_DIR, "rules.json")
LOG_FILE = os.path.join(BASE_DIR, "reply_log.csv")
os.makedirs(PROFILES_DIR, exist_ok=True)


def load_rules():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"accounts": [
        {"name": "账号1", "enabled": True,
         "rules": [{"keyword": "在吗", "reply": "在的，有什么可以帮您？"},
                    {"keyword": "利率", "reply": "您好！利率请拨打96688咨询~"},
                    {"keyword": "贷款", "reply": "您好！贷款请到网点或拨打96688咨询~"}],
         "phone_reply": "好的，已收到您的手机号，稍后联系您~",
         "default_reply": "您好！感谢关注遵义农商银行，请拨打96688或到网点咨询~",
         "poll_interval": 5}
    ]}


def save_rules(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def find_chrome():
    if sys.platform == "win32":
        for p in [
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
            os.path.expandvars("%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"),
        ]:
            if os.path.exists(p): return p
    return None


def get_driver_path():
    exe = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
    local = os.path.join(BASE_DIR, exe)
    if os.path.exists(local): return local
    import shutil
    d = shutil.which(exe)
    return d


class AccountWorker(QThread):
    log_signal = pyqtSignal(str, str, str)  # name, type, message
    status_signal = pyqtSignal(str, str)

    def __init__(self, acc, idx):
        super().__init__()
        self.acc = acc
        self.idx = idx
        self.name = acc["name"]
        self.rules = acc.get("rules", [])
        self.ph_reply = acc.get("phone_reply", "")
        self.def_reply = acc.get("default_reply", "")
        self.poll = acc.get("poll_interval", 5)
        self._stop = False
        self._replied = set()
        self._log_rows = []

    def stop(self):
        self._stop = True

    def _log(self, t, msg):
        self.log_signal.emit(self.name, t, msg)

    def _match(self, text):
        if self.ph_reply and re.search(r'1[3-9]\d{9}', text):
            return self.ph_reply, "手机号"
        for r in self.rules:
            kw = r.get("keyword", "")
            if kw and kw in text:
                return r["reply"], f"关键词[{kw}]"
        if self.def_reply:
            return self.def_reply, "默认"
        return None, None

    def _write_log(self, sender, msg_in, msg_out):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log_rows.append([ts, self.name, sender, msg_in, msg_out])
        try:
            existed = os.path.exists(LOG_FILE)
            with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not existed:
                    w.writerow(["时间", "账号", "联系人", "收到消息", "回复内容"])
                w.writerow([ts, self.name, sender, msg_in, msg_out])
        except:
            pass

    def run(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError:
            self._log("error", "缺少 selenium"); return

        chrome = find_chrome()
        if not chrome:
            self._log("error", "未找到 Chrome 浏览器"); return

        profile = os.path.join(PROFILES_DIR, f"acc_{self.idx}")
        os.makedirs(profile, exist_ok=True)

        opts = Options()
        opts.binary_location = chrome
        opts.add_argument(f"--user-data-dir={profile}")
        opts.add_argument("--no-first-run")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("detach", True)

        drv = get_driver_path()
        svc = Service(executable_path=drv) if drv else Service()
        try:
            driver = webdriver.Chrome(service=svc, options=opts)
        except Exception as e:
            self._log("error", f"Chrome启动失败: {e}"); return

        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        driver.set_window_size(500, 800)

        try:
            driver.get("https://www.douyin.com")
            self._log("info", "Chrome已打开，请扫码登录，再点进「消息」页面")
            self.status_signal.emit(self.name, "等待登录")

            time.sleep(8)
            self.status_signal.emit(self.name, "监控中")
            self._log("info", "✅ 开始监控")

            while not self._stop:
                try:
                    # 1. 确保在消息列表页
                    self._ensure_message_list(driver)

                    # 2. 扫描红点
                    reds = self._scan_red_dots(driver)

                    for red in reds:
                        if self._stop: break
                        cid = red["id"]
                        if cid in self._replied:
                            continue

                        self._replied.add(cid)

                        # 3. 点击对话
                        ok = self._click_conversation(driver, red["index"])
                        if not ok: continue
                        time.sleep(2)

                        # 4. 读最后一条消息
                        last_msg = self._read_last_message(driver)

                        # 5. 匹配并回复
                        reply, rule = self._match(last_msg)
                        if reply:
                            self._send_reply(driver, reply)
                            sender = red.get("name", "用户")
                            self._log("reply", f"📩 {sender}: {last_msg[:30]} → 📤 {reply[:30]}")
                            self._write_log(sender, last_msg, reply)
                        time.sleep(1)

                        # 6. 退回列表
                        self._go_back(driver)
                        time.sleep(1)

                    time.sleep(self.poll)

                except Exception as e:
                    self._log("warn", f"异常: {str(e)[:80]}")
                    time.sleep(self.poll)

        except Exception as e:
            self._log("error", f"{e}")
        finally:
            try: driver.quit()
            except: pass
            self._log("info", "已停止")
            self.status_signal.emit(self.name, "已停止")

    def _ensure_message_list(self, driver):
        """如果不在消息列表，尝试导航回去"""
        try:
            url = driver.current_url or ""
            if "messages" not in url and "im" not in url:
                try:
                    driver.get("https://www.douyin.com/messages")
                    time.sleep(3)
                except:
                    pass
        except:
            pass

    def _scan_red_dots(self, driver):
        """扫描消息列表中的红点，跳过群聊，返回红点列表"""
        js = """
        let reds = [];
        try {
            let items = document.querySelectorAll('[class*="conversation"], [class*="session"], div[class*="Cov"]');
            for (let i = 0; i < items.length; i++) {
                let el = items[i];
                let text = (el.textContent || '').trim();
                // 跳过群聊标识
                if (text.includes('群聊') || text.includes('（') || el.querySelector('[class*="group"]')) continue;
                let badge = el.querySelector('sup, [class*="badge"], [class*="unread"], [class*="count"]');
                if (badge) {
                    let t = badge.textContent.trim();
                    if (t && /\\d/.test(t)) {
                        // 提取对话ID（data属性或别的唯一标识）
                        let cid = el.getAttribute('data-conversation-id') || el.getAttribute('data-id') || ('idx_' + i);
                        // 提取对方名称
                        let nameEl = el.querySelector('[class*="name"], [class*="nickname"], span');
                        let name = nameEl ? nameEl.textContent.trim().substring(0,20) : '用户';
                        reds.push({id: cid, index: i, name: name, unread: t});
                    }
                }
            }
        } catch(e) {}
        return JSON.stringify(reds);
        """
        raw = driver.execute_script(js)
        return json.loads(raw) if raw else []

    def _click_conversation(self, driver, index):
        """点击指定索引的对话"""
        return driver.execute_script(f"""
            try {{
                let items = document.querySelectorAll('[class*="conversation"], [class*="session"], div[class*="Cov"]');
                if (items.length > {index}) {{
                    items[{index}].click();
                    return true;
                }}
            }} catch(e) {{}}
            return false;
        """)

    def _read_last_message(self, driver):
        """读取对话中最后一条对方消息"""
        return driver.execute_script("""
            try {
                let msgs = document.querySelectorAll('[class*="message"], [class*="msg"], div[class*="bubble"], div[class*="text"]');
                if (msgs.length > 0) {
                    return msgs[msgs.length-1].textContent.trim();
                }
            } catch(e) {}
            return '';
        """)

    def _send_reply(self, driver, text):
        """发送回复"""
        driver.execute_script(f"""
            let r = {json.dumps(text, ensure_ascii=False)};
            try {{
                let inp = document.querySelector('textarea, [contenteditable="true"], div[contenteditable]');
                if (!inp) inp = document.querySelector('[class*="input"], [class*="editor"]');
                if (inp) {{
                    if (inp.tagName === 'TEXTAREA') inp.value = r;
                    else inp.textContent = r;
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                }}
            }} catch(e) {{}}
            JSON.stringify({{ok: true}});
        """)
        time.sleep(0.5)
        driver.execute_script("""
            try {
                let inp = document.querySelector('textarea, [contenteditable="true"]');
                if (inp) inp.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',code:'Enter',bubbles:true}));
                let btn = document.querySelector('[class*="send"]');
                if (btn) btn.click();
            } catch(e) {}
        """)

    def _go_back(self, driver):
        """退回消息列表"""
        driver.execute_script("""
            try {
                let back = document.querySelector('[class*="back"], [class*="return"], svg[class*="arrow"]');
                if (back) back.closest('div,button,span').click();
                else history.back();
            } catch(e) {}
        """)


# ============= GUI =============

STYLE = """
QMainWindow{background:#1e1e1e}
QTabWidget::pane{border:1px solid #333;background:#252525}
QTabBar::tab{background:#2d2d2d;color:#aaa;padding:8px 16px;border:none}
QTabBar::tab:selected{background:#c41230;color:#fff}
QTableWidget{background:#1a1a1a;color:#ddd;gridline-color:#333;border:1px solid #333}
QHeaderView::section{background:#2d2d2d;color:#aaa;border:none;padding:4px}
QLineEdit,QTextEdit{background:#2d2d2d;color:#ddd;border:1px solid #444;padding:4px;border-radius:4px}
QPushButton{background:#3a3a3a;color:#ddd;border:none;padding:6px 14px;border-radius:4px}
QPushButton:hover{background:#4a4a4a}
QPushButton#btnStart{background:#c41230;color:#fff;font-weight:bold}
QPushButton#btnStart:hover{background:#e01438}
QPushButton#btnStop{background:#555}
QPushButton#btnAdd{background:#25f4ee;color:#000}
QLabel{color:#bbb}
QGroupBox{color:#aaa;border:1px solid #333;border-radius:6px;margin-top:8px;padding-top:12px}
QGroupBox::title{subcontrol-origin:margin;left:12px}
QStatusBar{background:#2d2d2d;color:#aaa}
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("抖音多账号私信自动回复 - 遵义农商银行")
        self.setGeometry(100, 100, 1050, 720)
        self.setStyleSheet(STYLE)

        self.config = load_rules()
        self.workers = {}
        self.tabs = {}

        self._build_ui()
        self._refresh_tabs()
        self.statusBar().showMessage("就绪")

    def _build_ui(self):
        c = QWidget(); self.setCentralWidget(c)
        ml = QVBoxLayout(c)

        top = QHBoxLayout()
        b = QPushButton("+ 添加账号"); b.setObjectName("btnAdd"); b.clicked.connect(self._add_account); top.addWidget(b)
        b = QPushButton("💾 保存"); b.clicked.connect(self._save); top.addWidget(b)
        top.addStretch()
        self.bs = QPushButton("▶ 全部启动"); self.bs.setObjectName("btnStart"); self.bs.clicked.connect(self._start_all); top.addWidget(self.bs)
        b = QPushButton("⏹ 全部停止"); b.setObjectName("btnStop"); b.clicked.connect(self._stop_all); top.addWidget(b)
        ml.addLayout(top)

        self.tab_w = QTabWidget(); ml.addWidget(self.tab_w)

        g = QGroupBox("运行日志")
        vl = QVBoxLayout(g)
        btns = QHBoxLayout()
        b = QPushButton("导出日志CSV"); b.clicked.connect(self._export_log); btns.addWidget(b)
        b = QPushButton("清空日志"); b.clicked.connect(lambda: self.log_t.clear()); btns.addWidget(b)
        btns.addStretch(); vl.addLayout(btns)
        self.log_t = QTextEdit(); self.log_t.setReadOnly(True); self.log_t.setMaximumHeight(140)
        self.log_t.setFont(styleFont("Consolas")); vl.addWidget(self.log_t)
        ml.addWidget(g)

    def _refresh_tabs(self):
        self.tab_w.clear(); self.tabs.clear()
        for i, a in enumerate(self.config.get("accounts", [])): self._add_tab(i, a)

    def _add_tab(self, i, a):
        t = QWidget(); l = QVBoxLayout(t)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("名称:"))
        nm = QLineEdit(a.get("name", f"账号{i+1}")); r1.addWidget(nm)
        en = QCheckBox("启用"); en.setChecked(a.get("enabled", True)); r1.addWidget(en)
        r1.addStretch()
        st = QLabel("⚪ 未启动"); r1.addWidget(st)
        l.addLayout(r1)

        tb = QTableWidget(); tb.setColumnCount(3)
        tb.setHorizontalHeaderLabels(["关键词", "回复内容", ""])
        tb.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        tb.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        tb.setColumnWidth(0, 120); tb.setColumnWidth(2, 40)
        for j, r in enumerate(a.get("rules", [])):
            if j >= tb.rowCount(): tb.insertRow(j)
            tb.setItem(j, 0, QTableWidgetItem(r.get("keyword", "")))
            tb.setItem(j, 1, QTableWidgetItem(r.get("reply", "")))
        if tb.rowCount() == 0: tb.insertRow(0)

        l.addWidget(QLabel("关键词规则:")); l.addWidget(tb)

        rb = QHBoxLayout()
        b = QPushButton("+ 添加"); b.clicked.connect(lambda: tb.insertRow(tb.rowCount())); rb.addWidget(b)
        b = QPushButton("- 删除"); b.clicked.connect(lambda: (tb.currentRow() >= 0 and tb.rowCount() > 1) and tb.removeRow(tb.currentRow())); rb.addWidget(b)
        rb.addStretch(); l.addLayout(rb)

        r2 = QHBoxLayout(); r2.addWidget(QLabel("手机号回复:"))
        ph = QLineEdit(a.get("phone_reply", "")); r2.addWidget(ph); l.addLayout(r2)

        r3 = QHBoxLayout(); r3.addWidget(QLabel("默认回复:"))
        df = QLineEdit(a.get("default_reply", "")); r3.addWidget(df); l.addLayout(r3)

        r4 = QHBoxLayout(); r4.addWidget(QLabel("间隔(秒):"))
        pi = QLineEdit(str(a.get("poll_interval", 5))); pi.setMaximumWidth(50); r4.addWidget(pi)
        r4.addStretch()
        b = QPushButton("▶ 启动"); b.setObjectName("btnStart"); b.clicked.connect(lambda _, x=i: self._start(x)); r4.addWidget(b)
        b = QPushButton("⏹ 停止"); b.clicked.connect(lambda _, x=i: self._stop(x)); r4.addWidget(b)
        l.addLayout(r4)

        r5 = QHBoxLayout(); r5.addStretch()
        b = QPushButton("🗑 删除账号"); b.clicked.connect(lambda _, x=i: self._del(x)); r5.addWidget(b); l.addLayout(r5)

        self.tab_w.addTab(t, a.get("name", f"账号{i+1}"))
        self.tabs[i] = {"name": nm, "enabled": en, "status": st, "table": tb,
                        "phone": ph, "default": df, "poll": pi}

    def _add_account(self):
        n = len(self.config["accounts"]) + 1
        self.config["accounts"].append({
            "name": f"账号{n}", "enabled": True,
            "rules": [{"keyword": "在吗", "reply": "在的！"}],
            "phone_reply": "好的收到~", "default_reply": "您好！感谢关注遵义农商银行！", "poll_interval": 5
        })
        self._refresh_tabs()
        self._log("系统", "info", f"已添加账号{n}")

    def _del(self, i):
        if QMessageBox.question(self, "确认", f"删除{self.config['accounts'][i]['name']}？") == QMessageBox.Yes:
            self._stop(i); del self.config["accounts"][i]; save_rules(self.config); self._refresh_tabs()

    def _read(self, i):
        t = self.tabs[i]; tb = t["table"]; rules = []
        for r in range(tb.rowCount()):
            kw = tb.item(r, 0); rp = tb.item(r, 1)
            if kw and kw.text().strip(): rules.append({"keyword": kw.text().strip(), "reply": (rp.text() if rp else "")})
        return {"name": t["name"].text(), "enabled": t["enabled"].isChecked(), "rules": rules,
                "phone_reply": t["phone"].text(), "default_reply": t["default"].text(),
                "poll_interval": int(t["poll"].text()) if t["poll"].text().isdigit() else 5}

    def _save(self):
        for i in range(len(self.config["accounts"])): self.config["accounts"][i] = self._read(i)
        save_rules(self.config)

    def _start(self, i):
        self._save(); a = self.config["accounts"][i]
        if not a["enabled"] or a["name"] in self.workers: return
        w = AccountWorker(a, i)
        w.log_signal.connect(self._log); w.status_signal.connect(lambda n, s, j=i: self._upd(j, s))
        w.start(); self.workers[a["name"]] = w
        self.tabs[i]["status"].setText("🟡 等待登录...")

    def _stop(self, i):
        nm = self.config["accounts"][i]["name"]
        if nm in self.workers:
            self.workers[nm].stop(); self.workers[nm].wait(5000); del self.workers[nm]
        self.tabs[i]["status"].setText("⚪ 未启动")

    def _start_all(self):
        self._save()
        for i in range(len(self.config["accounts"])):
            if self.config["accounts"][i]["enabled"]: self._start(i)

    def _stop_all(self):
        for i in range(len(self.config["accounts"])): self._stop(i)

    def _upd(self, i, s):
        colors = {"监控中": "#25f4ee", "等待登录": "#ff9a44", "已停止": "#aaa"}
        self.tabs[i]["status"].setText(f"● {s}")
        self.tabs[i]["status"].setStyleSheet(f"color:{colors.get(s, '#aaa')};")

    def _log(self, name, t, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_t.append(f"[{ts}] [{name}] {msg}")

    def _export_log(self):
        if not os.path.exists(LOG_FILE):
            QMessageBox.information(self, "提示", "暂无日志"); return
        p, _ = QFileDialog.getSaveFileName(self, "导出日志", "回复记录.csv", "CSV(*.csv)")
        if p:
            import shutil; shutil.copy(LOG_FILE, p)
            self._log("系统", "info", f"日志已导出: {p}")

    def closeEvent(self, e):
        self._stop_all(); self._save(); e.accept()


def styleFont(f):
    ff = QFont(f, 9)
    if "微软雅黑" in [QFont(f).family() for f in ["Consolas"]]:
        return ff
    return QFont("Consolas", 9)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    MainWindow().show()
    sys.exit(app.exec_())
