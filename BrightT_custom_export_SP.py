# -*- coding: utf-8 -*-
"""BrightT_custom_export_SP — custom texture export for Substance 3D Painter.
Author: BrightT. Version: 1.3.0.
"""
__version__ = '1.3.1'
__author__ = 'BrightT'
APP_NAME = 'BrightT_custom_export_SP'

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


def numbered_name(name, number, segment=2, width=2):
    parts = name.split('_')
    if segment > len(parts):
        raise ValueError('名称 {} 没有第 {} 段'.format(name, segment))
    parts[segment - 1] += str(number).zfill(width)
    return '_'.join(parts)


def validate_name(value):
    if not value or value != value.strip() or value.endswith('.'):
        raise ValueError('名称不能为空，不能以空格或句点结尾：' + repr(value))
    if re.search(r'[<>:"/\\|?*$\x00-\x1f]', value):
        raise ValueError('名称包含路径字符、通配符或非法字符：' + value)
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        raise ValueError('Windows 保留名称：' + value)
    if len(value) > 180:
        raise ValueError('文件名过长：' + value)
    return value


def suggest_suffix(filename, index):
    # A suggestion only; arbitrary template syntax is replaced by a literal user suffix.
    text = re.sub(r'\$[A-Za-z][A-Za-z0-9]*', '', filename)
    text = re.sub(r'[(){}\[\]]', '', text).strip('_. -')
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text)
    return text or 'Map{:02d}'.format(index + 1)


def channel_summary(output_map, destination):
    channels = output_map.get('channels', [])
    matches = [c for c in channels if c.get('destChannel') == destination]
    if not matches and destination in 'RGB':
        matches = [c for c in channels if c.get('destChannel') == 'L']
    return '; '.join('{}:{} [{}]{}'.format(
        c.get('srcMapName', '?'), c.get('srcChannel', '?'), c.get('srcMapType', '?'),
        (' @ ' + c['srcPath']) if c.get('srcPath') else '') for c in matches) or '—'


def renamed_maps(row, maps, fmt=None, depth=None, size=None):
    """Deep-copy the entire native map, including RGBA, srcPath and extra fields."""
    validate_name(row['base'])
    output_maps = []
    for source_map, suffix in maps:
        output = deepcopy(source_map)
        output['fileName'] = validate_name(row['base'] + '_' + validate_name(suffix))
        parameters = {'fileFormat': 'png', 'bitDepth': '8', 'dithering': False,
                      'paddingAlgorithm': 'infinite'}
        parameters.update(output.get('parameters', {}))
        if fmt is not None:
            parameters['fileFormat'] = fmt
        if depth is not None:
            parameters['bitDepth'] = depth
        if size is not None:
            parameters['sizeLog2'] = size
        if parameters['fileFormat'] == 'tga' and parameters['bitDepth'] != '8':
            raise ValueError('TGA 请选择 8 位；或改用其他格式')
        output['parameters'] = parameters
        output_maps.append(output)
    if not output_maps:
        raise ValueError('请至少勾选一个模板输出')
    return output_maps


def make_config(row, maps, directory, fmt=None, depth=None, size=None):
    output_maps = renamed_maps(row, maps, fmt, depth, size)
    entry = {'rootPath': row['root']}
    if row['tile'] is not None:
        entry['filter'] = {'uvTiles': [list(row['tile'])]}
    return {'exportPath': str(directory), 'exportShaderParams': False,
            'defaultExportPreset': 'CustomNames',
            'exportPresets': [{'name': 'CustomNames', 'maps': output_maps}],
            'exportList': [entry]}


def expected_names(rows, maps, fmt=None, depth=None, size=None):
    names = []
    for row in rows:
        for output in renamed_maps(row, maps, fmt, depth, size):
            extension = output['parameters']['fileFormat']
            extension = {'jpeg': 'jpg', 'tiff': 'tif'}.get(extension, extension)
            names.append(validate_name(output['fileName'] + '.' + extension))
    if not names:
        raise ValueError('请选择导出行和贴图类型')
    seen = set()
    for name in names:
        key = name.casefold()
        if key in seen:
            raise ValueError('导出文件重名，请修改名称或后缀：' + name)
        seen.add(key)
    return names


def row_key(row):
    return row['root'], tuple(row['tile'] or [])


def restore_rows(current, saved):
    """Restore order/settings by source identity, never by row position."""
    remaining = {row_key(r): dict(r) for r in current}
    result = []
    for old in saved:
        row = remaining.pop(row_key(old), None)
        if row is not None:
            row.update({k: old[k] for k in ('base', 'enabled') if k in old})
            result.append(row)
    result.extend(remaining.values())
    return result


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_profiles(path):
    if not Path(path).exists():
        return {'version': 1, 'default': None, 'profiles': {}}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('profiles'), dict):
        raise ValueError('导出配置文件格式不支持：' + str(path))
    return data


def publish_files(stage, destination, names, overwrite=False):
    """Back up existing files before publishing; roll back a failed batch."""
    stage, destination = Path(stage), Path(destination)
    existing = {p.name.casefold(): p for p in destination.iterdir()}
    tasks, changed = [], []
    seen = set()
    for name in names:
        validate_name(name)
        if name.casefold() in seen:
            raise ValueError('文件重名：' + name)
        seen.add(name.casefold())
        source = stage / name
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError('导出文件缺失或为空：' + name)
        target = existing.get(name.casefold(), destination / name)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError('目标不是普通文件：' + str(target))
        if target.exists() and not overwrite:
            raise FileExistsError('已有同名文件：' + str(target))
        tasks.append((source, target, target.exists()))
    backup_dir = stage / '_brightt_backup'
    backups = {}
    if any(exists for _, _, exists in tasks):
        backup_dir.mkdir()
        for i, (_, target, exists) in enumerate(tasks):
            if exists:
                backup = backup_dir / str(i)
                shutil.copy2(target, backup)
                backups[target] = backup
    try:
        for source, target, exists in tasks:
            if exists:
                os.replace(source, target)
                changed.append(target)
            else:
                with target.open('xb') as out:
                    changed.append(target)
                    with source.open('rb') as inp:
                        shutil.copyfileobj(inp, out)
    except Exception:
        failures = []
        for target in reversed(changed):
            try:
                if target in backups:
                    os.replace(backups[target], target)
                else:
                    target.unlink()
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError('回滚未全部完成，原文件备份保留在 {}：{}'.format(backup_dir, '; '.join(failures)))
        raise



THEME = """
QDialog#BrightTWindow, QWidget#Content { background: #151229; color: #eeeaf8; }
QWidget { font-family: "Segoe UI", "Microsoft YaHei UI"; font-size: 12px; color: #eeeaf8; }
QScrollArea { background: transparent; border: none; }
QFrame#Card { background: #211b38; border: 1px solid #362b50; border-radius: 16px; }
QLabel { background: transparent; border: none; }
QLabel#AppTitle { font-size: 23px; font-weight: 600; color: #f5f1ff; }
QLabel#SectionTitle { font-size: 15px; font-weight: 600; color: #f2ecff; padding-right: 12px; }
QLabel#Muted { color: #b5abc9; font-size: 11px; }
QLabel#Logo { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #aa80ff,stop:1 #6534d8); border-radius: 17px; color: white; font-size: 22px; font-weight: 700; }
QLabel#Badge { background: #2b2146; color: #cbb4ff; border: 1px solid #443262; border-radius: 12px; padding: 9px 14px; }
QPushButton { background: #302547; border: 1px solid #4c3969; border-radius: 9px; padding: 8px 14px; color: #e6dafb; min-height: 18px; }
QPushButton:hover { background: #453263; border-color: #a47aff; }
QPushButton:pressed { background: #574075; }
QPushButton:disabled { color: #80758e; background: #211b34; border-color: #30273e; }
QPushButton#Primary { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #9966fa,stop:1 #683ce5); border: 1px solid #ac85ff; color: white; font-weight: 600; }
QPushButton#Primary:hover { background: #9b6dff; }
QLineEdit, QSpinBox, QComboBox { background: #18142c; border: 1px solid #47365f; border-radius: 8px; padding: 7px 10px; min-height: 18px; selection-background-color: #784cc7; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #ab83ff; }
QSpinBox { padding-right: 24px; min-width: 45px; }
QCheckBox { background: transparent; spacing: 7px; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #251d3c; color: #eeeaf8; selection-background-color: #6543a1; border: 1px solid #6f5099; }
QTableWidget { background: #1b172e; alternate-background-color: #221c36; border: 1px solid #352a4b; border-radius: 9px; selection-background-color: #4a356f; selection-color: white; }
QTableWidget::item { padding: 8px 10px; border-bottom: 1px solid #30263f; }
QTableWidget::indicator { width: 14px; height: 14px; border-radius: 4px; border: 1px solid #6b538b; background: #211b35; }
QTableWidget::indicator:checked { background: #9868f7; border: 2px solid #d1b6ff; }
QHeaderView::section { background: #2c2342; color: #c4b5df; border: none; padding: 10px 9px; font-weight: 600; }
QPlainTextEdit#StatusLog { background: #1c172e; border: 1px solid #352a49; border-radius: 10px; padding: 6px; color: #baadd2; font-size: 11px; }
QScrollBar:vertical { background: #19142b; width: 8px; margin: 0; border-radius: 4px; }
QScrollBar::handle:vertical { background: #574175; min-height: 26px; border-radius: 4px; }
QScrollBar:horizontal { background: #19142b; height: 8px; margin: 0; }
QScrollBar::handle:horizontal { background: #574175; min-width: 26px; border-radius: 4px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QToolTip { color: #f4edff; background: #38294f; border: 1px solid #9670c8; padding: 6px; }
"""

try:
    import substance_painter as sp
    import substance_painter.application
    import substance_painter.export
    import substance_painter.project
    import substance_painter.textureset
    import substance_painter.ui
except ModuleNotFoundError as exc:
    if exc.name != 'substance_painter':
        raise
    sp = None  # Allows testing pure naming/export-planning functions outside Painter.


if sp is not None:
    if sp.application.version_info() < (10, 1, 0):
        from PySide2 import QtWidgets, QtCore, QtGui
        Action = QtWidgets.QAction
    else:
        from PySide6 import QtWidgets, QtCore, QtGui
        Action = QtGui.QAction

    class SpinArrowButton(QtWidgets.QToolButton):
        def __init__(self, direction, parent):
            super().__init__(parent)
            self.direction = direction
            self.setFocusPolicy(QtCore.Qt.NoFocus)
            self.setAutoRepeat(True)
            self.setAccessibleName('增加' if direction < 0 else '减少')
            self.setStyleSheet('QToolButton { border: 0; padding: 0; margin: 0; min-width: 0; min-height: 0; }')

        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor('#d0b5ff' if self.underMouse() else '#b49acf'))
            painter.drawRoundedRect(self.rect(), 3, 3)
            painter.setBrush(QtGui.QColor('#26163d'))
            center = self.rect().center()
            painter.drawPolygon(QtGui.QPolygon([
                center + QtCore.QPoint(-4, -self.direction * 2),
                center + QtCore.QPoint(4, -self.direction * 2),
                center + QtCore.QPoint(0, self.direction * 3)]))
            painter.end()

        def wheelEvent(self, event):
            event.ignore()

    class ArrowSpinBox(QtWidgets.QSpinBox):
        def __init__(self, parent=None):
            super().__init__(parent)
            # Own the button geometry: Painter's host style can distort native subcontrols.
            self.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
            self.up_button = SpinArrowButton(-1, self)
            self.down_button = SpinArrowButton(1, self)
            self.up_button.clicked.connect(self.stepUp)
            self.down_button.clicked.connect(self.stepDown)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            height = self.height() - 4
            half = height // 2
            self.up_button.setGeometry(self.width() - 23, 2, 20, half)
            self.down_button.setGeometry(self.width() - 23, 2 + half, 20, height - half)

        def wheelEvent(self, event):
            event.ignore()

    class NoWheelComboBox(QtWidgets.QComboBox):
        def wheelEvent(self, event):
            event.ignore()

    class ReorderTable(QtWidgets.QTableWidget):
        rowMoved = QtCore.Signal(int, int)

        def startDrag(self, supported):
            self.drag_row = self.currentRow()
            if self.drag_row < 0:
                return
            drag = QtGui.QDrag(self)
            mime = QtCore.QMimeData()
            mime.setData('application/x-brightt-row', b'row')
            drag.setMimeData(mime)
            run = getattr(drag, 'exec', None) or drag.exec_
            run(QtCore.Qt.MoveAction)

        def dragEnterEvent(self, event):
            if event.source() is self:
                event.acceptProposedAction()
            else:
                event.ignore()

        def dragMoveEvent(self, event):
            self.dragEnterEvent(event)

        def dropEvent(self, event):
            if event.source() is not self:
                event.ignore()
                return
            point = event.position().toPoint() if hasattr(event, 'position') else event.pos()
            target = self.rowAt(point.y())
            if target < 0:
                target = self.rowCount() - 1
            self.rowMoved.emit(self.drag_row, target)
            event.acceptProposedAction()

    class ExportDialog(QtWidgets.QDialog):
        def __init__(self):
            super().__init__(sp.ui.get_main_window())
            self.setWindowTitle(APP_NAME + ' · ' + __version__)
            self.setObjectName('BrightTWindow')
            self.setWindowModality(QtCore.Qt.ApplicationModal)
            self.resize(1320, 960)
            self.setMinimumSize(1040, 720)
            self.rows, self.native_maps, self.presets = [], [], []
            self.updating = True
            self.setStyleSheet(THEME)
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(24, 20, 24, 18)
            outer.setSpacing(16)
            header = QtWidgets.QHBoxLayout()
            logo = QtWidgets.QLabel('BT')
            logo.setObjectName('Logo')
            logo.setAlignment(QtCore.Qt.AlignCenter)
            logo.setFixedSize(54, 54)
            header.addWidget(logo)
            brand = QtWidgets.QVBoxLayout()
            title = QtWidgets.QLabel(APP_NAME)
            title.setObjectName('AppTitle')
            brand.addWidget(title)
            subtitle = QtWidgets.QLabel('自定义通道 · 独立命名 · 即时预览    /    制作者 BrightT')
            subtitle.setObjectName('Muted')
            brand.addWidget(subtitle)
            header.addLayout(brand)
            header.addStretch()
            self.count_label = QtWidgets.QLabel('等待项目')
            self.count_label.setObjectName('Badge')
            header.addWidget(self.count_label)
            version = QtWidgets.QLabel('v' + __version__)
            version.setObjectName('Badge')
            header.addWidget(version)
            outer.addLayout(header)

            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            content = QtWidgets.QWidget()
            content.setObjectName('Content')
            layout = QtWidgets.QVBoxLayout(content)
            layout.setContentsMargins(0, 0, 6, 0)
            layout.setSpacing(14)
            scroll.setWidget(content)
            outer.addWidget(scroll, 1)

            template = self.card(layout, '输出模板', '沿用 SP 保存的 RGB / Alpha 组合，导出内容由模板决定。')
            presetbar = QtWidgets.QHBoxLayout()
            template.addLayout(presetbar)
            self.preset = NoWheelComboBox()
            self.preset.setMinimumWidth(330)
            presetbar.addWidget(self.preset, 1)
            self.button(presetbar, '刷新列表', self.refresh_presets)
            self.button(presetbar, '重新读取模板', self.load_preset)
            profilebar = QtWidgets.QHBoxLayout()
            template.addLayout(profilebar)
            profilebar.addWidget(QtWidgets.QLabel('导出配置'))
            self.profile = NoWheelComboBox()
            profilebar.addWidget(self.profile, 1)
            self.button(profilebar, '另存配置', self.save_profile_as)
            self.button(profilebar, '更新配置', self.update_profile)
            self.button(profilebar, '重命名', self.rename_profile)
            self.button(profilebar, '删除', self.delete_profile)
            self.default_profile = QtWidgets.QCheckBox('启动默认')
            profilebar.addWidget(self.default_profile)

            naming = self.card(layout, '命名与预览', '拖动或移动行调整顺序，再点击批量编号。')
            bar = QtWidgets.QHBoxLayout()
            naming.addLayout(bar)
            self.button(bar, '刷新材质 / UDIM', self.refresh)
            bar.addWidget(QtWidgets.QLabel('起始'))
            self.start = ArrowSpinBox()
            self.start.setRange(0, 99999)
            self.start.setValue(1)
            bar.addWidget(self.start)
            bar.addWidget(QtWidgets.QLabel('位数'))
            self.width = ArrowSpinBox()
            self.width.setRange(1, 6)
            self.width.setValue(2)
            bar.addWidget(self.width)
            bar.addWidget(QtWidgets.QLabel('追加至第几段'))
            self.segment = ArrowSpinBox()
            self.segment.setRange(1, 20)
            self.segment.setValue(2)
            bar.addWidget(self.segment)
            self.button(bar, '批量编号', self.number)
            bar.addStretch()
            self.button(bar, '载入命名表', self.load_mapping)
            self.button(bar, '保存命名表', self.save_mapping)
            orderbar = QtWidgets.QHBoxLayout()
            naming.addLayout(orderbar)
            self.button(orderbar, '↑ 上移', lambda: self.move_selected(-1))
            self.button(orderbar, '↓ 下移', lambda: self.move_selected(1))
            self.button(orderbar, '置顶', lambda: self.move_row(self.table.currentRow(), 0))
            self.button(orderbar, '置底', lambda: self.move_row(self.table.currentRow(), len(self.rows) - 1))
            orderbar.addStretch()
            orderbar.addWidget(QtWidgets.QLabel('移动保留手填名称；编号按当前行顺序。'))
            self.table = ReorderTable(0, 5)
            self.table.setHorizontalHeaderLabels(['导出', '材质 / Stack', 'UDIM', '基础名称', '修改后的文件名预览'])
            self.prepare_table(self.table, 210)
            self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
            self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.table.setDragEnabled(True)
            self.table.setAcceptDrops(True)
            self.table.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
            self.table.rowMoved.connect(self.move_row)
            self.table.setColumnWidth(0, 52)
            self.table.setColumnWidth(1, 210)
            self.table.setColumnWidth(2, 74)
            self.table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
            self.table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
            naming.addWidget(self.table)

            channels = self.card(layout, '输出与通道', '勾选需要的输出并修改后缀；RGB / Alpha 来源在 SP 原生输出模板中编辑。')
            self.outputs = QtWidgets.QTableWidget(0, 7)
            self.outputs.setHorizontalHeaderLabels(['导出', '模板原文件名', '后缀', 'R', 'G', 'B', 'Alpha'])
            self.prepare_table(self.outputs, 152)
            self.outputs.setColumnWidth(0, 52)
            self.outputs.setColumnWidth(1, 240)
            self.outputs.setColumnWidth(2, 90)
            for col in range(3, 7):
                self.outputs.horizontalHeader().setSectionResizeMode(col, QtWidgets.QHeaderView.Stretch)
            channels.addWidget(self.outputs)

            destination = self.card(layout, '导出设置', '勾选替换时覆盖已有同名贴图；未勾选时停止。')
            options = QtWidgets.QHBoxLayout()
            destination.addLayout(options)
            self.fmt = self.combo(options, '格式', ['跟随模板', 'png', 'tga', 'tif', 'exr'])
            self.depth = self.combo(options, '位深', ['跟随模板', '8', '16', '16f', '32f'])
            self.size = self.combo(options, '分辨率', ['跟随模板 / 项目', '512', '1024', '2048', '4096', '8192'])
            pathbar = QtWidgets.QHBoxLayout()
            destination.addLayout(pathbar)
            pathbar.addWidget(QtWidgets.QLabel('目录'))
            self.path = QtWidgets.QLineEdit()
            self.path.setPlaceholderText('选择贴图保存目录…')
            pathbar.addWidget(self.path, 1)
            self.button(pathbar, '浏览目录', self.choose_directory)
            self.overwrite = QtWidgets.QCheckBox('替换同名文件')
            self.overwrite.setChecked(False)
            pathbar.addWidget(self.overwrite)

            footer = QtWidgets.QHBoxLayout()
            self.log = QtWidgets.QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumHeight(64)
            self.log.setObjectName('StatusLog')
            footer.addWidget(self.log, 1)
            self.button(footer, 'SP 校验预览', self.preview)
            primary = self.button(footer, '导出贴图  →', self.export)
            primary.setObjectName('Primary')
            primary.setMinimumWidth(150)
            primary.setMinimumHeight(46)
            outer.addLayout(footer)
            self.refresh()
            self.refresh_presets()
            self.updating = False
            self.table.itemChanged.connect(self.changed)
            self.outputs.itemChanged.connect(self.changed)
            self.preset.currentIndexChanged.connect(lambda _: self.guard(self.load_preset))
            for combo in (self.fmt, self.depth, self.size):
                combo.currentIndexChanged.connect(self.changed)
            self.path.textChanged.connect(self.changed)
            self.overwrite.toggled.connect(self.changed)
            self.profile_path = Path(QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.GenericConfigLocation)) / APP_NAME / 'profiles.json'
            self.profile_db = {'version': 1, 'default': None, 'profiles': {}}
            self.profile_read_error = False
            self.profile.currentIndexChanged.connect(lambda _: self.guard(self.select_profile))
            self.default_profile.toggled.connect(lambda checked: self.guard(lambda: self.set_default_profile(checked)))
            self.changed()
            self.guard(self.initialize_profiles)

        def card(self, parent, title, description):
            frame = QtWidgets.QFrame()
            frame.setObjectName('Card')
            box = QtWidgets.QVBoxLayout(frame)
            box.setContentsMargins(18, 14, 18, 16)
            box.setSpacing(12)
            head = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel(title)
            label.setObjectName('SectionTitle')
            head.addWidget(label)
            hint = QtWidgets.QLabel(description)
            hint.setObjectName('Muted')
            head.addWidget(hint)
            head.addStretch()
            box.addLayout(head)
            parent.addWidget(frame)
            return box

        def prepare_table(self, table, height):
            table.setMinimumHeight(height)
            table.setShowGrid(False)
            table.setAlternatingRowColors(True)
            table.verticalHeader().hide()
            table.verticalHeader().setDefaultSectionSize(42)
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectItems)
            table.setWordWrap(False)
            table.horizontalHeader().setMinimumSectionSize(48)

        def button(self, layout, label, callback):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda checked=False: self.guard(callback))
            layout.addWidget(button)
            return button

        def combo(self, layout, label, choices):
            group = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            caption = QtWidgets.QLabel(label)
            caption.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred)
            row.addWidget(caption)
            combo = NoWheelComboBox()
            combo.addItems(choices)
            row.addWidget(combo, 1)
            layout.setSpacing(20)
            layout.addWidget(group, 1)
            return combo

        def guard(self, callback):
            try:
                callback()
            except Exception as exc:
                self.log.appendPlainText('错误：' + str(exc))
                QtWidgets.QMessageBox.warning(self, '未完成', str(exc))

        def readonly(self, text):
            item = QtWidgets.QTableWidgetItem(text)
            item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsDragEnabled)
            item.setToolTip(text)
            return item

        def snapshot_rows(self):
            return [dict(row, base=self.table.item(i, 3).text(),
                         enabled=self.table.item(i, 0).checkState() == QtCore.Qt.Checked)
                    for i, row in enumerate(self.rows)]

        def render_rows(self, rows):
            self.table.blockSignals(True)
            self.rows = rows
            self.table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                enabled = QtWidgets.QTableWidgetItem()
                enabled.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsSelectable)
                enabled.setCheckState(QtCore.Qt.Checked if row.get('enabled', True) else QtCore.Qt.Unchecked)
                self.table.setItem(i, 0, enabled)
                tile = row['tile']
                udim = str(1001 + tile[0] + tile[1] * 10) if tile is not None else '—'
                self.table.setItem(i, 1, self.readonly(row['root']))
                self.table.setItem(i, 2, self.readonly(udim))
                self.table.setItem(i, 3, QtWidgets.QTableWidgetItem(row.get('base', row['material'])))
            self.table.blockSignals(False)
            self.changed()

        def move_row(self, source, target):
            if not 0 <= source < len(self.rows) or not 0 <= target < len(self.rows):
                return
            rows = self.snapshot_rows()
            rows.insert(target, rows.pop(source))
            self.render_rows(rows)
            self.table.selectRow(target)

        def move_selected(self, offset):
            source = self.table.currentRow()
            self.move_row(source, source + offset)

        def capture_config(self):
            return {'preset_url': self.preset.currentData(), 'outputs': [
                {'original': m['fileName'], 'suffix': self.outputs.item(i, 2).text(),
                 'enabled': self.outputs.item(i, 0).checkState() == QtCore.Qt.Checked}
                for i, m in enumerate(self.native_maps)],
                'format': self.fmt.currentText(), 'depth': self.depth.currentText(),
                'size': self.size.currentText(), 'directory': self.path.text(),
                'overwrite': self.overwrite.isChecked(), 'start': self.start.value(),
                'width': self.width.value(), 'segment': self.segment.value()}

        def apply_config(self, config):
            index = self.preset.findData(config.get('preset_url'))
            if index < 0:
                raise ValueError('配置引用的 SP 输出模板不存在，请导入该模板并刷新列表。')
            # Validate the entire configuration before modifying the UI.
            maps = self.presets[index].list_output_maps()
            output_names = {m['fileName'] for m in maps}
            if any(o['original'] not in output_names for o in config.get('outputs', [])):
                raise ValueError('SP 模板的输出列表已改变，请重新选择模板并更新导出配置。')
            for key, combo in [('format', self.fmt), ('depth', self.depth), ('size', self.size)]:
                if key in config and combo.findText(config[key]) < 0:
                    raise ValueError('配置中的设置不支持：' + key)
            previous = self.updating
            self.updating = True
            try:
                self.preset.blockSignals(True)
                self.preset.setCurrentIndex(index)
                self.preset.blockSignals(False)
                self.load_preset()
                saved = {m['original']: m for m in config.get('outputs', [])}
                for i, m in enumerate(self.native_maps):
                    entry = saved.get(m['fileName'])
                    self.outputs.item(i, 0).setCheckState(QtCore.Qt.Checked if entry and entry.get('enabled', True) else QtCore.Qt.Unchecked)
                    if entry:
                        self.outputs.item(i, 2).setText(entry['suffix'])
                for key, combo in [('format', self.fmt), ('depth', self.depth), ('size', self.size)]:
                    if key in config:
                        combo.setCurrentIndex(combo.findText(config[key]))
                if 'directory' in config:
                    self.path.setText(config['directory'])
                self.overwrite.setChecked(bool(config.get('overwrite', False)))
                for key, control in [('start', self.start), ('width', self.width), ('segment', self.segment)]:
                    if key in config:
                        control.setValue(int(config[key]))
            finally:
                self.updating = previous
            self.changed()

        def initialize_profiles(self):
            try:
                self.profile_db = read_profiles(self.profile_path)
            except Exception:
                self.profile_read_error = True
                raise
            default = self.profile_db.get('default')
            self.refresh_profile_list(default)
            if default:
                self.select_profile()

        def refresh_profile_list(self, select=None):
            self.profile.blockSignals(True)
            self.profile.clear()
            self.profile.addItem('临时配置（未保存）', None)
            for name in self.profile_db['profiles']:
                label = name + ('  ★ 默认' if name == self.profile_db.get('default') else '')
                self.profile.addItem(label, name)
            index = self.profile.findData(select) if select else 0
            self.profile.setCurrentIndex(max(0, index))
            self.profile.blockSignals(False)
            self.default_profile.blockSignals(True)
            self.default_profile.setChecked(bool(select) and select == self.profile_db.get('default'))
            self.default_profile.blockSignals(False)

        def commit_profiles(self, db, select):
            if self.profile_read_error:
                raise ValueError('配置文件读取失败，为保护原文件已禁止写入：' + str(self.profile_path))
            write_json_atomic(self.profile_path, db)
            self.profile_db = db
            self.refresh_profile_list(select)

        def select_profile(self):
            name = self.profile.currentData()
            if name:
                self.apply_config(self.profile_db['profiles'][name])
            self.refresh_profile_list(name)

        def save_profile_as(self):
            name, ok = QtWidgets.QInputDialog.getText(self, '另存导出配置', '配置名称')
            name = name.strip()
            if not ok or not name:
                return
            if name in self.profile_db['profiles']:
                raise ValueError('名称已存在，请使用“更新配置”或换一个名称。')
            db = deepcopy(self.profile_db)
            db['profiles'][name] = self.capture_config()
            self.commit_profiles(db, name)

        def update_profile(self):
            name = self.profile.currentData()
            if not name:
                raise ValueError('请先另存配置，或选择已有配置。')
            db = deepcopy(self.profile_db)
            db['profiles'][name] = self.capture_config()
            self.commit_profiles(db, name)
            self.log.appendPlainText('已更新配置：' + name)

        def rename_profile(self):
            old = self.profile.currentData()
            if not old:
                raise ValueError('请先选择已保存的配置。')
            name, ok = QtWidgets.QInputDialog.getText(self, '重命名配置', '新名称', text=old)
            name = name.strip()
            if not ok or not name or name == old:
                return
            if name in self.profile_db['profiles']:
                raise ValueError('名称已存在。')
            db = deepcopy(self.profile_db)
            db['profiles'][name] = db['profiles'].pop(old)
            if db.get('default') == old:
                db['default'] = name
            self.commit_profiles(db, name)

        def delete_profile(self):
            name = self.profile.currentData()
            if not name:
                return
            if QtWidgets.QMessageBox.question(self, '删除配置', '删除配置“{}”？当前界面设置仍保留。'.format(name)) != QtWidgets.QMessageBox.Yes:
                return
            db = deepcopy(self.profile_db)
            del db['profiles'][name]
            if db.get('default') == name:
                db['default'] = None
            self.commit_profiles(db, None)

        def set_default_profile(self, checked):
            name = self.profile.currentData()
            if checked and not name:
                self.refresh_profile_list(None)
                raise ValueError('请先另存配置，再设为启动默认。')
            db = deepcopy(self.profile_db)
            db['default'] = name if checked else None
            self.commit_profiles(db, name)

        def refresh_presets(self):
            if not hasattr(sp.export, 'list_resource_export_presets'):
                raise ValueError('当前 SP 不提供输出模板读取接口，请升级 SP 后使用本插件。')
            old = self.preset.currentData()
            self.presets = sorted(sp.export.list_resource_export_presets(),
                                  key=lambda p: (p.resource_id.name, p.resource_id.url()))
            self.preset.blockSignals(True)
            self.preset.clear()
            for preset in self.presets:
                rid = preset.resource_id
                self.preset.addItem(rid.name, rid.url())
                self.preset.setItemData(self.preset.count() - 1, rid.url(), QtCore.Qt.ToolTipRole)
            index = self.preset.findData(old) if old else -1
            if index < 0:
                index = next((i for i, p in enumerate(self.presets)
                              if p.resource_id.name == 'PBR Metallic Roughness'), 0)
            if self.presets:
                self.preset.setCurrentIndex(index)
            self.preset.blockSignals(False)
            self.load_preset()

        def load_preset(self):
            self.native_maps = []
            self.outputs.setRowCount(0)
            index = self.preset.currentIndex()
            if index < 0:
                self.log.setPlainText('没有可读取的输出模板。请先在 SP 中保存一个输出模板。')
                self.changed()
                return
            maps = self.presets[index].list_output_maps()
            self.outputs.blockSignals(True)
            try:
                self.native_maps = deepcopy(maps)
                self.outputs.setRowCount(len(maps))
                for i, output in enumerate(maps):
                    enabled = QtWidgets.QTableWidgetItem()
                    enabled.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable)
                    enabled.setCheckState(QtCore.Qt.Checked)
                    self.outputs.setItem(i, 0, enabled)
                    self.outputs.setItem(i, 1, self.readonly(output['fileName']))
                    self.outputs.setItem(i, 2, QtWidgets.QTableWidgetItem(suggest_suffix(output['fileName'], i)))
                    for col, channel in enumerate('RGBA', 3):
                        self.outputs.setItem(i, col, self.readonly(channel_summary(output, channel)))
            finally:
                self.outputs.blockSignals(False)
            self.log.setPlainText('已读取 {} 个模板输出。法线方向、RGB/Alpha 来源由模板决定。'.format(len(maps)))
            self.changed()

        def settings(self):
            maps = [(output, self.outputs.item(i, 2).text()) for i, output in enumerate(self.native_maps)
                    if self.outputs.item(i, 0).checkState() == QtCore.Qt.Checked]
            fmt = self.fmt.currentText() if self.fmt.currentIndex() else None
            depth = self.depth.currentText() if self.depth.currentIndex() else None
            size = self.size.currentIndex() + 8 if self.size.currentIndex() else None
            return maps, fmt, depth, size

        def changed(self, *args):
            if self.updating:
                return
            self.table.blockSignals(True)
            try:
                maps, fmt, depth, size = self.settings()
                all_names = {}
                previews = {}
                for i, row in enumerate(self.rows):
                    if self.table.item(i, 0).checkState() != QtCore.Qt.Checked:
                        previews[i] = ('未勾选', False)
                        continue
                    try:
                        names = expected_names([dict(row, base=self.table.item(i, 3).text())], maps, fmt, depth, size)
                        previews[i] = ('\n'.join(names), False)
                        for name in names:
                            all_names.setdefault(name.casefold(), set()).add(i)
                    except Exception as exc:
                        previews[i] = ('错误：' + str(exc), True)
                duplicates = {i for owners in all_names.values() if len(owners) > 1 for i in owners}
                for i, (text, error) in previews.items():
                    if i in duplicates:
                        text = '⚠ 文件重名\n' + text
                    item = self.readonly(text)
                    item.setForeground(QtGui.QColor('#ff9eae' if error or i in duplicates else '#c9b5ff'))
                    item.setBackground(QtGui.QColor('#2b2345'))
                    self.table.setItem(i, 4, item)
                self.count_label.setText('{} 个导出项  ·  {} 张贴图'.format(
                    len(self.selected()), sum(len(owners) for owners in all_names.values())))
                self.table.resizeRowsToContents()
                self.log.setPlainText('命名预览已更新，尚未通过 SP 校验。请选择目录后点击“SP 校验预览”。')
            finally:
                self.table.blockSignals(False)

        def refresh(self):
            if not sp.project.is_open():
                raise ValueError('请先打开 SP 项目')
            saved = self.snapshot_rows()
            rows = []
            for material in sorted(sp.textureset.all_texture_sets(), key=lambda m: m.name()):
                tiles = sorted(material.all_uv_tiles(), key=lambda t: (t.v, t.u)) if material.has_uv_tiles() else [None]
                for stack in material.all_stacks():
                    for tile in tiles:
                        rows.append({'root': str(stack), 'material': material.name(),
                                     'tile': [tile.u, tile.v] if tile is not None else None})
            self.render_rows(restore_rows(rows, saved))

        def number(self):
            edits = []
            n = self.start.value()
            for i, row in enumerate(self.rows):
                if self.table.item(i, 0).checkState() == QtCore.Qt.Checked:
                    edits.append((i, numbered_name(row['material'], n, self.segment.value(), self.width.value())))
                    n += 1
            for i, name in edits:
                self.table.item(i, 3).setText(name)

        def selected(self):
            rows = []
            for i, row in enumerate(self.rows):
                if self.table.item(i, 0).checkState() == QtCore.Qt.Checked:
                    rows.append(dict(row, base=self.table.item(i, 3).text()))
            return rows

        def plan(self):
            if not sp.project.is_open():
                raise ValueError('请先打开项目')
            directory = self.path.text().strip()
            if not directory or not Path(directory).is_absolute():
                raise ValueError('请选择绝对路径的导出目录')
            maps, fmt, depth, size = self.settings()
            rows = self.selected()
            names = expected_names(rows, maps, fmt, depth, size)
            configs = [make_config(row, maps, directory, fmt, depth, size) for row in rows]
            # Ask Painter to validate tile filtering and literal filenames before writing anything.
            actual = []
            for config in configs:
                paths = [p for group in sp.export.list_project_textures(config).values() for p in group]
                actual.extend(Path(p).name for p in paths)
            if sorted(actual) != sorted(names):
                raise ValueError('SP 预览结果与命名表不一致，已停止。请确认贴图通道存在、UV Tile 工作流正确。\n'
                                 '预期：' + ', '.join(names) + '\nSP：' + ', '.join(actual))
            existing = {p.name.casefold() for p in Path(directory).iterdir()} if Path(directory).exists() else set()
            clashes = [n for n in names if n.casefold() in existing]
            if clashes and not self.overwrite.isChecked():
                raise ValueError('目录已有同名文件，请使用新目录：\n' + '\n'.join(clashes))
            return configs, names, Path(directory)

        def preview(self):
            _, names, directory = self.plan()
            self.log.setPlainText('共 {} 张 → {}\n同名文件：{}\n{}'.format(len(names), directory, '替换（已勾选）' if self.overwrite.isChecked() else '不替换', '\n'.join(names)))

        def export(self):
            configs, names, directory = self.plan()
            directory.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.sp_custom_', dir=str(directory)))
            self.setEnabled(False)
            try:
                for config in configs:
                    config['exportPath'] = str(stage)
                    result = sp.export.export_project_textures(config)
                    if result.status != sp.export.ExportStatus.Success:
                        raise RuntimeError('SP 返回 {}：{}'.format(result.status.name, result.message))
                actual = sorted(p.name for p in stage.iterdir() if p.is_file())
                if actual != sorted(names):
                    raise RuntimeError('实际输出与预览不一致，未发布文件')
                publish_files(stage, directory, names, self.overwrite.isChecked())
                shutil.rmtree(str(stage))
                self.log.setPlainText('导出完成：{} 张\n{}\n{}'.format(len(names), directory, '\n'.join(names)))
            except Exception as exc:
                raise RuntimeError('{}\n中间文件保留在：{}'.format(exc, stage)) from exc
            finally:
                self.setEnabled(True)

        def choose_directory(self):
            value = QtWidgets.QFileDialog.getExistingDirectory(self, '选择导出目录', self.path.text())
            if value:
                self.path.setText(value)

        def save_mapping(self):
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, '保存命名表', 'texture_names.json', 'JSON (*.json)')
            if path:
                write_json_atomic(path, {'version': 3, 'rows': self.snapshot_rows(),
                                         'config': self.capture_config()})
                self.log.appendPlainText('已保存行顺序、命名和导出设置：' + path)

        def load_mapping(self):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '载入命名表', '', 'JSON (*.json)')
            if path:
                data = json.loads(Path(path).read_text(encoding='utf-8'))
                if data.get('version') not in (1, 2, 3):
                    raise ValueError('不支持的命名表版本')
                if data['version'] == 3:
                    self.apply_config(data['config'])
                elif data['version'] == 2:
                    self.apply_config(data)
                self.render_rows(restore_rows(self.snapshot_rows(), data['rows']))
                matched = len({row_key(r) for r in self.rows} & {row_key(r) for r in data['rows']})
                self.log.appendPlainText('已恢复 {} 行的顺序与命名；新增行追加在末尾。'.format(matched))


_action = None
_dialog = None


def open_dialog():
    global _dialog
    if not sp.project.is_open():
        QtWidgets.QMessageBox.information(sp.ui.get_main_window(), APP_NAME, '请先打开 SP 项目。')
        return
    if _dialog is not None:
        _dialog.close()
        _dialog.deleteLater()
    _dialog = ExportDialog()
    _dialog.show()


def start_plugin():
    global _action
    if _action is None:
        _action = Action(APP_NAME + '…', triggered=open_dialog)
        sp.ui.add_action(sp.ui.ApplicationMenu.File, _action)


def close_plugin():
    global _action, _dialog
    if _dialog is not None:
        _dialog.close()
        _dialog.deleteLater()
        _dialog = None
    if _action is not None:
        sp.ui.delete_ui_element(_action)
        _action = None


if __name__ == '__main__' and sp is not None:
    start_plugin()
