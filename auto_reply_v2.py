# -*- coding: utf-8 -*-
"""
抖音多账号自动回复 v22 — 仅陌生人 + 退出生成报告
====================================================
流程：
  1. 扫码登录 → 跳转私信页 → 检测「陌生人消息」→ 点击进入
  2. 停留在陌生人列表 → 检测红点 → 自动回复
  3. 退出时 → 弹出提示 → 返回主私信页 → 抓取每个陌生人的回复(手机号等)
  → 导出Excel到桌面
"""
import os, sys, re, json, time, csv, threading
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QFileDialog, QGroupBox, QCheckBox, QStatusBar
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(BASE_DIR, "chrome_profiles")
CONFIG_FILE = os.path.join(BASE_DIR, "rules.json")
CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
POLL = 5
DESKTOP = os.path.expanduser("~/Desktop")

os.makedirs(PROFILES_DIR, exist_ok=True)


def load_rules():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"accounts": [
        {"name": "账号1", "enabled": True,
         "reply_text": "您好！感谢关注遵义农商银行，请问您是在遵义市吗？如需办理业务请留下您的联系方式，我们将安排客户经理与您联系~",
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
    return shutil.which(exe)


class AccountWorker(QThread):
    log_signal = pyqtSignal(str, str)
    status_signal = pyqtSignal(str, str)
    report_ready = pyqtSignal(str, str)  # account_name, filepath

    def __init__(self, acc, idx):
        super().__init__()
        self.acc = acc
        self.idx = idx
        self.name = acc["name"]
        self.reply_text = acc.get("reply_text", "")
        self.poll = acc.get("poll_interval", POLL)
        self._stop = False
        self._login_ok = threading.Event()
        self._driver = None
        # 今天回复过的所有陌生人: {昵称: {"first_msg": 对方第一条消息, "my_reply": 我方回复}}
        self.today_strangers = {}
        self._in_stranger = False

    def confirm_login(self):
        self._login_ok.set()

    def stop(self):
        self._stop = True

    def log(self, msg):
        self.log_signal.emit(self.name, msg)

    def run(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError:
            self.log("缺少 selenium"); return

        chrome = find_chrome()
        if not chrome:
            self.log("未找到 Chrome"); return

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
            self.log(f"Chrome启动失败: {e}"); return

        self._driver = driver
        driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        driver.set_window_size(500, 800)

        try:
            driver.get("https://www.douyin.com")
            self.log("请扫码登录，完成后点「确认已登录」")
            self.status_signal.emit(self.name, "等待登录")

            while not self._login_ok.is_set() and not self._stop:
                time.sleep(0.5)
            if self._stop: return

            # 跳转到聊天页
            driver.get(CHAT_URL)
            time.sleep(3)
            self.status_signal.emit(self.name, "监控中")
            self.log("✅ 等待陌生人消息...")
            last_reply_time = {}

            while not self._stop:
                # ─── 尝试进入陌生人消息 ───
                if not self._in_stranger:
                    clicked = driver.execute_script("""
                        let items = document.querySelectorAll('[class*="conversation"], [class*="session"], [class*="ConversationItem"]');
                        for (let el of items) {
                            if ((el.textContent||'').includes('陌生人消息')) {
                                el.focus();
                                ['mousedown','mouseup','click'].forEach(e =>
                                    el.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true}))
                                );
                                return true;
                            }
                        }
                        return false;
                    """)
                    if clicked:
                        time.sleep(3)
                        self._in_stranger = True
                        self.log("🚪 进入陌生人消息")
                    else:
                        time.sleep(self.poll)
                        continue

                # ─── 陌生人列表内扫描红点 ───
                reds = self._scan_reds(driver)

                for red in reds:
                    if self._stop: break
                    name = red.get("name", "用户")
                    now = time.time()
                    if name in last_reply_time and now - last_reply_time[name] < 30:
                        continue

                    self.log(f"📨 陌生人: {name}")

                    # 点击对话
                    ok = driver.execute_script("""
                        let badges = document.querySelectorAll('span[class*="ConversationItemUnRead"]');
                        for (let b of badges) {
                            let t = b.textContent.trim();
                            if (!t || !/^\\d+$/.test(t)) continue;
                            let item = b;
                            for (let d=0; d<10 && item; d++) {
                                item = item.parentElement;
                                if (!item) break;
                                if (item.className && item.className.includes('conversationConversationItem')) {
                                    item.focus();
                                    ['mousedown','mouseup','click'].forEach(e =>
                                        item.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true}))
                                    );
                                    return true;
                                }
                            }
                        }
                        return false;
                    """)
                    if not ok: continue
                    time.sleep(2)

                    # 读对方第一条消息
                    first_msg = driver.execute_script("""
                        let all = document.querySelectorAll('div[class*="message-content"], div[class*="bubble"], div[class*="msg-text"], span[class*="content"]');
                        for (let i=all.length-1; i>=0; i--) {
                            let t = all[i].textContent.trim();
                            if (t.length>0 && t.length<500 && !t.includes('发送')) return t;
                        }
                        return '';
                    """)

                    # 记录陌生人信息
                    if name not in self.today_strangers:
                        self.today_strangers[name] = {"first_msg": first_msg, "my_reply": self.reply_text}

                    # 回复
                    if self.reply_text and self._send_reply(driver, self.reply_text):
                        self.log(f"📤 已回复: {self.reply_text[:30]}...")
                        last_reply_time[name] = time.time()

                    self._back_to_list(driver)
                    time.sleep(1)

                time.sleep(self.poll)

        except Exception as e:
            self.log(f"异常: {e}")
        finally:
            if self._driver:
                self._generate_report()
                try: self._driver.quit()
                except: pass
            self.status_signal.emit(self.name, "已停止")

    def _scan_reds(self, driver):
        raw = driver.execute_script("""
            let reds=[];let idx=0;
            try{
                let badges=document.querySelectorAll('span[class*="ConversationItemUnRead"]');
                badges.forEach(b=>{
                    let t=b.textContent.trim();
                    if(!t||!/^\\d+$/.test(t))return;
                    let item=b;
                    for(let d=0;d<10&&item;d++){
                        item=item.parentElement;if(!item)break;
                        if(item.className&&item.className.includes('conversationConversationItem')){
                            let name=(item.textContent||'').split(/[\\s\\n]/)[0].substring(0,15);
                            reds.push({name:name,unread:t});idx++;break;
                        }
                    }
                });
            }catch(e){}
            return JSON.stringify(reds);
        """)
        result = json.loads(raw) if raw else []
        return [r for r in result if isinstance(r, dict)]

    def _back_to_list(self, driver):
        driver.execute_script("""
            let back=document.querySelector('[class*="back"], [class*="return"], [class*="arrow"]');
            if(back){back.closest('div,button,span').click();return;}
            let tabs=document.querySelectorAll('[class*="tab"] span, [class*="nav"] div');
            for(let t of tabs){if(/消息/.test(t.textContent)){t.click();return;}}
        """)

    def _send_reply(self, driver, text):
        found = driver.execute_script("""
            let inp=null;
            let all=document.querySelectorAll('div[contenteditable="true"], textarea');
            for(let el of all){
                let r=el.getBoundingClientRect();
                if(r.height>20&&r.height<200&&r.top>window.innerHeight*0.35){inp=el;break;}
            }
            if(!inp)inp=document.querySelector('div[data-placeholder]')||document.querySelector('div[class*="rich-input"]');
            if(inp){inp.focus();inp.click();}
            return !!inp;
        """)
        if not found: return False
        time.sleep(0.3)
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
        actions = ActionChains(driver)
        for ch in text: actions.send_keys(ch)
        actions.pause(0.3).send_keys(Keys.ENTER).perform()
        return True

    def _generate_report(self):
        """退出时：回到主私信页 → 抓每个陌生人的后续回复 → 生成Excel"""
        if not self.today_strangers or not self._driver:
            return

        names = list(self.today_strangers.keys())
        if not names:
            return

        driver = self._driver
        self.log(f"📊 生成报告: {len(names)} 个陌生人")

        # 回到私信首页
        driver.get(CHAT_URL)
        time.sleep(3)

        follow_up = {}

        for name in names:
            try:
                # 在主私信列表搜索该用户并点击
                found = driver.execute_script("""
                    let items = document.querySelectorAll('[class*="conversation"], [class*="session"], [class*="ConversationItem"]');
                    for (let el of items) {
                        if ((el.textContent||'').includes(arguments[0])) {
                            el.focus();
                            ['mousedown','mouseup','click'].forEach(e =>
                                el.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true}))
                            );
                            return true;
                        }
                    }
                    return false;
                """, name)

                if found:
                    time.sleep(2)
                    # 读取对方后来发的所有消息
                    msgs = driver.execute_script("""
                        let results = [];
                        let all = document.querySelectorAll('div[class*="message-content"], div[class*="bubble"], div[class*="msg-text"], span[class*="content"]');
                        for (let el of all) {
                            let t = el.textContent.trim();
                            if (t.length > 0 && t.length < 500 && !t.includes('发送')) {
                                results.push(t);
                            }
                        }
                        return results;
                    """)
                    # 只取超出第一条以外的消息（对方的后续回复）
                    if msgs and len(msgs) > 1:
                        follow_up[name] = " | ".join(msgs[1:])
                    else:
                        follow_up[name] = ""

                    self._back_to_list(driver)
                    time.sleep(1)
            except:
                follow_up[name] = ""

        # 生成Excel/CSV
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(DESKTOP, f"陌生人回复记录_{self.name}_{ts}.csv")

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["序号", "陌生人昵称", "对方消息", "我方回复", "对方后续回复"])
            for i, name in enumerate(names, 1):
                info = self.today_strangers[name]
                w.writerow([
                    i,
                    name,
                    info.get("first_msg", ""),
                    info.get("my_reply", ""),
                    follow_up.get(name, "")
                ])

        self.log(f"📁 报告已保存: {os.path.basename(filepath)}")
        self.report_ready.emit(self.name, filepath)


# ===== GUI =====

STYLE = """
QMainWindow{background:#1e1e1e}QTabWidget::pane{border:1px solid #333;background:#252525}
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
        self.setWindowTitle("抖音多账号自动回复 v22 · 仅陌生人 - 遵义农商银行")
        self.setGeometry(100, 100, 1050, 620)
        self.setStyleSheet(STYLE)
        self.config = load_rules()
        self.workers = {}
        self.tabs = {}
        self._build_ui()
        self._refresh_tabs()
        self.statusBar().showMessage("就绪")

    def _build_ui(self):
        c = QWidget(); self.setCentralWidget(c); ml = QVBoxLayout(c)
        top = QHBoxLayout()
        b = QPushButton("+ 添加账号"); b.setObjectName("btnAdd"); b.clicked.connect(self._add_account); top.addWidget(b)
        b = QPushButton("💾 保存"); b.clicked.connect(self._save); top.addWidget(b)
        top.addStretch()
        b = QPushButton("▶ 全部启动"); b.setObjectName("btnStart"); b.clicked.connect(self._start_all); top.addWidget(b)
        b = QPushButton("⏹ 全部停止"); b.setObjectName("btnStop"); b.clicked.connect(self._stop_all); top.addWidget(b)
        ml.addLayout(top)
        self.tab_w = QTabWidget(); ml.addWidget(self.tab_w)
        g = QGroupBox("运行日志"); vl = QVBoxLayout(g)
        self.log_t = QTextEdit(); self.log_t.setReadOnly(True); self.log_t.setMaximumHeight(140)
        self.log_t.setFont(QFont("Consolas", 9)); vl.addWidget(self.log_t)
        ml.addWidget(g)

    def _refresh_tabs(self):
        self.tab_w.clear(); self.tabs.clear()
        for i, a in enumerate(self.config.get("accounts", [])): self._add_tab(i, a)

    def _add_tab(self, i, a):
        t = QWidget(); l = QVBoxLayout(t)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("名称:")); nm = QLineEdit(a.get("name", f"账号{i+1}")); r1.addWidget(nm)
        en = QCheckBox("启用"); en.setChecked(a.get("enabled", True)); r1.addWidget(en); r1.addStretch()
        st = QLabel("⚪ 未启动"); r1.addWidget(st); l.addLayout(r1)

        r2 = QHBoxLayout(); r2.addWidget(QLabel("回复内容:"))
        rp = QLineEdit(a.get("reply_text", "")); r2.addWidget(rp); l.addLayout(r2)

        r4 = QHBoxLayout()
        r4.addWidget(QLabel("间隔(秒):")); pi = QLineEdit(str(a.get("poll_interval", 5)))
        pi.setMaximumWidth(50); r4.addWidget(pi); r4.addStretch()
        b = QPushButton("▶ 启动"); b.setObjectName("btnStart"); b.clicked.connect(lambda _, x=i: self._start(x)); r4.addWidget(b)
        b = QPushButton("✓ 确认已登录"); b.setStyleSheet("background:#25f4ee;color:#000;font-weight:bold;")
        b.clicked.connect(lambda _, x=i: self._confirm_login(x)); r4.addWidget(b)
        b = QPushButton("⏹ 停止"); b.clicked.connect(lambda _, x=i: self._stop(x)); r4.addWidget(b)
        l.addLayout(r4)

        r5 = QHBoxLayout(); r5.addStretch(); b = QPushButton("🗑 删除账号")
        b.clicked.connect(lambda _, x=i: self._del(x)); r5.addWidget(b); l.addLayout(r5)

        self.tab_w.addTab(t, a.get("name", f"账号{i+1}"))
        self.tabs[i] = {"name": nm, "enabled": en, "status": st, "reply": rp, "poll": pi}

    def _add_account(self):
        n = len(self.config["accounts"]) + 1
        self.config["accounts"].append({
            "name": f"账号{n}", "enabled": True,
            "reply_text": "您好！感谢关注遵义农商银行，请问您是在遵义市吗？如需办理业务请留下您的联系方式~",
            "poll_interval": 5
        })
        self._refresh_tabs(); self._log("系统", f"已添加账号{n}")

    def _del(self, i):
        if QMessageBox.question(self, "确认", f"删除{self.config['accounts'][i]['name']}？") == QMessageBox.Yes:
            self._stop(i); del self.config["accounts"][i]; save_rules(self.config); self._refresh_tabs()

    def _read(self, i):
        t = self.tabs[i]
        return {
            "name": t["name"].text(), "enabled": t["enabled"].isChecked(),
            "reply_text": t["reply"].text(),
            "poll_interval": int(t["poll"].text()) if t["poll"].text().isdigit() else 5
        }

    def _save(self):
        for i in range(len(self.config["accounts"])): self.config["accounts"][i] = self._read(i)
        save_rules(self.config)

    def _start(self, i):
        self._save(); a = self.config["accounts"][i]
        if not a["enabled"] or a["name"] in self.workers: return
        w = AccountWorker(a, i)
        w.log_signal.connect(self._log)
        w.status_signal.connect(lambda n, s, j=i: self._upd(j, s))
        w.report_ready.connect(self._on_report)
        w.start(); self.workers[a["name"]] = w; self.tabs[i]["status"].setText("🟡 等待登录...")

    def _stop(self, i):
        nm = self.config["accounts"][i]["name"]
        if nm in self.workers: self.workers[nm].stop(); self.workers[nm].wait(5000); del self.workers[nm]
        self.tabs[i]["status"].setText("⚪ 未启动")

    def _start_all(self):
        self._save()
        for i in range(len(self.config["accounts"])):
            if self.config["accounts"][i]["enabled"]: self._start(i)

    def _stop_all(self):
        for i in range(len(self.config["accounts"])): self._stop(i)

    def _confirm_login(self, idx):
        nm = self.config["accounts"][idx]["name"]
        if nm in self.workers:
            self.workers[nm].confirm_login()
            self.tabs[idx]["status"].setText("🟢 监控中")
            self.tabs[idx]["status"].setStyleSheet("color:#25f4ee;")
            self._log("系统", "确认登录，开始监控")

    def _upd(self, i, s):
        colors = {"监控中": "#25f4ee", "等待登录": "#ff9a44", "已停止": "#aaa"}
        self.tabs[i]["status"].setText(f"● {s}")
        self.tabs[i]["status"].setStyleSheet(f"color:{colors.get(s, '#aaa')};")

    def _log(self, name, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_t.append(f"[{ts}] [{name}] {msg}")

    def _on_report(self, name, filepath):
        self._log(name, f"📁 报告: {os.path.basename(filepath)}")

    def closeEvent(self, e):
        if self.workers:
            reply = QMessageBox.question(self, "退出确认",
                "退出前将自动保存所有陌生人的聊天记录到桌面。\n\n确定退出？",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.No:
                e.ignore(); return
        self._stop_all(); self._save(); e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion"); MainWindow().show(); sys.exit(app.exec_())
