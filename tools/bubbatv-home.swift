import Cocoa

@MainActor
final class ActionButton: NSButton {
    var handler: (() -> Void)?
    convenience init(_ title: String, symbol: String? = nil, action: @escaping () -> Void) {
        self.init(frame: .zero)
        self.title = title
        bezelStyle = .rounded
        if let symbol { image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil); imagePosition = .imageLeading }
        target = self
        self.action = #selector(invoke)
        handler = action
    }
    @objc private func invoke() { handler?() }
}

@MainActor
final class MediaDropView: NSView {
    var onDrop: (([URL]) -> Void)?
    var enabled = true
    private var highlighted = false { didSet { needsDisplay = true } }

    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes([.fileURL])
        setAccessibilityIdentifier("media-drop-area")
        setAccessibilityLabel("Drop media here to play it")
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func fileURLs(from pasteboard: NSPasteboard) -> [URL] {
        (pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
    }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        highlighted = enabled && fileURLs(from: sender.draggingPasteboard).contains { mediaExists($0.path) }
        return highlighted ? .copy : []
    }
    override func draggingExited(_ sender: NSDraggingInfo?) { highlighted = false }
    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool { enabled && highlighted }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = fileURLs(from: sender.draggingPasteboard).filter { mediaExists($0.path) }
        highlighted = false
        guard enabled && !urls.isEmpty else { return false }
        onDrop?(urls)
        return true
    }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let shape = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 18, yRadius: 18)
        NSColor.controlAccentColor.withAlphaComponent(highlighted ? 0.12 : 0.035).setFill()
        shape.fill()
        (highlighted ? NSColor.controlAccentColor : NSColor.separatorColor).setStroke()
        shape.lineWidth = highlighted ? 2 : 1
        shape.setLineDash([7, 5], count: 2, phase: 0)
        shape.stroke()
    }
}

@MainActor
private final class RecentTable: NSTableView {
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 36 && selectedRow >= 0, let action = doubleAction {
            NSApp.sendAction(action, to: target, from: self)
        } else { super.keyDown(with: event) }
    }
}

@MainActor
final class HomeWindow: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {
    var onOpen: (() -> Void)?
    var onPlay: ((String) -> Void)?
    var onResume: (() -> Void)?
    var onClear: (() -> Void)?
    var onRemove: ((String) -> Void)?
    var onDrop: (([URL]) -> Void)?
    private(set) var items: [RecentItem] = []
    private let table = RecentTable()
    let dropArea = MediaDropView(frame: .zero)
    private let continueBox = NSBox()
    private let continueTitle = NSTextField(labelWithString: "")
    private let continuePosition = NSTextField(labelWithString: "")
    private let empty = NSTextField(labelWithString: "Your recent media will appear here.")
    private let status = NSTextField(labelWithString: "Only files and folders available on disk are shown.")
    private let spinner = NSProgressIndicator()
    private var openButton: ActionButton!
    private var clearButton: ActionButton!
    private(set) var busy = false

    init() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 650),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "bubbaTV"
        window.minSize = NSSize(width: 620, height: 590)
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("bubbaTV Home")
        super.init(window: window)
        build()
        window.center()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    private func text(_ value: String, size: CGFloat, weight: NSFont.Weight = .regular, secondary: Bool = false) -> NSTextField {
        let label = NSTextField(labelWithString: value)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = secondary ? .secondaryLabelColor : .labelColor
        label.lineBreakMode = .byTruncatingMiddle
        return label
    }

    private func build() {
        guard let content = window?.contentView else { return }
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 18
        root.detachesHiddenViews = true
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 28),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -28),
            root.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20)
        ])
        let icon = NSImageView(image: NSApp.applicationIconImage)
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.widthAnchor.constraint(equalToConstant: 58).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 58).isActive = true
        let brand = NSStackView(views: [text("bubbaTV", size: 26, weight: .bold),
                                      text("Two languages. One movie.", size: 13, secondary: true)])
        brand.orientation = .vertical
        brand.alignment = .leading
        brand.spacing = 3
        let header = NSStackView(views: [icon, brand])
        header.spacing = 12
        root.addArrangedSubview(header)

        continueBox.boxType = .custom
        continueBox.borderWidth = 0
        continueBox.cornerRadius = 12
        continueBox.fillColor = .controlBackgroundColor
        continueBox.contentViewMargins = NSSize(width: 14, height: 12)
        continueTitle.font = .systemFont(ofSize: 14, weight: .semibold)
        continueTitle.lineBreakMode = .byTruncatingMiddle
        continuePosition.font = .systemFont(ofSize: 12)
        continuePosition.textColor = .secondaryLabelColor
        let continueLabels = NSStackView(views: [continueTitle, continuePosition])
        continueLabels.orientation = .vertical
        continueLabels.alignment = .leading
        continueLabels.spacing = 4
        let resume = ActionButton("Resume", symbol: "play.fill") { [weak self] in
            guard let self, !self.busy else { return }; self.onResume?()
        }
        resume.identifier = NSUserInterfaceItemIdentifier("resume-media")
        let continueRow = NSStackView(views: [continueLabels, resume])
        continueRow.spacing = 16
        continueRow.distribution = .fill
        continueLabels.setContentHuggingPriority(.defaultLow, for: .horizontal)
        continueLabels.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let continueContent = NSView()
        continueBox.contentView = continueContent
        continueRow.translatesAutoresizingMaskIntoConstraints = false
        continueContent.addSubview(continueRow)
        NSLayoutConstraint.activate([
            continueRow.leadingAnchor.constraint(equalTo: continueContent.leadingAnchor),
            continueRow.trailingAnchor.constraint(equalTo: continueContent.trailingAnchor),
            continueRow.topAnchor.constraint(equalTo: continueContent.topAnchor),
            continueRow.bottomAnchor.constraint(equalTo: continueContent.bottomAnchor)
        ])
        continueBox.heightAnchor.constraint(equalToConstant: 72).isActive = true
        root.addArrangedSubview(continueBox)

        let dropIcon = NSImageView(image: NSImage(systemSymbolName: "play.rectangle", accessibilityDescription: nil)!)
        dropIcon.contentTintColor = .controlAccentColor
        dropIcon.imageScaling = .scaleProportionallyUpOrDown
        dropIcon.widthAnchor.constraint(equalToConstant: 44).isActive = true
        dropIcon.heightAnchor.constraint(equalToConstant: 36).isActive = true
        openButton = ActionButton("Open Media…", symbol: "folder") { [weak self] in self?.onOpen?() }
        openButton.identifier = NSUserInterfaceItemIdentifier("open-media")
        openButton.bezelColor = .controlAccentColor
        openButton.keyEquivalent = "\r"
        openButton.toolTip = "Choose media files or a folder of episodes (⌘O)"
        let dropLabels = NSStackView(views: [dropIcon, text("Drop media here to play it", size: 19, weight: .semibold),
                                           text("Movies, audio, or a folder of episodes", size: 13, secondary: true), openButton])
        dropLabels.orientation = .vertical
        dropLabels.alignment = .centerX
        dropLabels.spacing = 10
        dropLabels.translatesAutoresizingMaskIntoConstraints = false
        dropArea.addSubview(dropLabels)
        NSLayoutConstraint.activate([
            dropLabels.centerXAnchor.constraint(equalTo: dropArea.centerXAnchor),
            dropLabels.centerYAnchor.constraint(equalTo: dropArea.centerYAnchor),
            dropLabels.leadingAnchor.constraint(greaterThanOrEqualTo: dropArea.leadingAnchor, constant: 12),
            dropArea.heightAnchor.constraint(equalToConstant: 204)
        ])
        dropArea.onDrop = { [weak self] in self?.onDrop?($0) }
        root.addArrangedSubview(dropArea)

        clearButton = ActionButton("Clear") { [weak self] in self?.onClear?() }
        clearButton.identifier = NSUserInterfaceItemIdentifier("clear-recents")
        clearButton.toolTip = "Clear the recent media list. Your files are kept."
        let recentHeading = text("Recent media", size: 15, weight: .semibold)
        recentHeading.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let heading = NSStackView(views: [recentHeading, clearButton])
        heading.distribution = .fill
        root.addArrangedSubview(heading)

        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("media"))
        column.resizingMask = .autoresizingMask
        table.addTableColumn(column)
        table.frame = NSRect(x: 0, y: 0, width: 704, height: 0)
        table.autoresizingMask = [.width]
        table.headerView = nil
        table.rowHeight = 62
        table.intercellSpacing = NSSize(width: 0, height: 4)
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.dataSource = self
        table.delegate = self
        table.target = self
        table.doubleAction = #selector(playSelected)
        table.identifier = NSUserInterfaceItemIdentifier("recent-media")
        table.setAccessibilityLabel("Recent media. Double-click or press Return to play.")
        table.usesAlternatingRowBackgroundColors = false
        table.backgroundColor = .clear
        let menu = NSMenu()
        for (title, action) in [("Play", #selector(playContextSelection)), ("Show in Finder", #selector(revealSelected)),
                                ("Remove from Recents", #selector(removeSelected))] {
            let item = menu.addItem(withTitle: title, action: action, keyEquivalent: "")
            item.target = self
        }
        table.menu = menu
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 95).isActive = true
        root.addArrangedSubview(scroll)
        empty.font = .systemFont(ofSize: 13)
        empty.textColor = .secondaryLabelColor
        empty.translatesAutoresizingMaskIntoConstraints = false
        scroll.addSubview(empty)
        NSLayoutConstraint.activate([empty.centerXAnchor.constraint(equalTo: scroll.centerXAnchor),
                                     empty.centerYAnchor.constraint(equalTo: scroll.centerYAnchor)])
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        status.font = .systemFont(ofSize: 11)
        status.textColor = .secondaryLabelColor
        let footer = NSStackView(views: [spinner, status])
        footer.spacing = 8
        root.addArrangedSubview(footer)
        for view in [continueBox, dropArea, heading, scroll, footer] {
            view.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        }
    }

    func refresh(items: [RecentItem], last: LastPlayback?) {
        self.items = items
        table.reloadData()
        empty.isHidden = !items.isEmpty
        clearButton.isEnabled = !items.isEmpty && !busy
        continueBox.isHidden = last?.canResume != true
        if let last, last.canResume {
            continueTitle.stringValue = "Continue · " + last.displayTitle
            continuePosition.stringValue = last.position
        }
    }

    func setBusy(_ value: Bool) {
        busy = value
        openButton.isEnabled = !value
        clearButton.isEnabled = !value && !items.isEmpty
        dropArea.enabled = !value
        if value { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        status.stringValue = value ? "Preparing playback…" : "Only files and folders available on disk are shown."
    }

    func numberOfRows(in tableView: NSTableView) -> Int { items.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let item = items[row]
        let url = URL(fileURLWithPath: item.path)
        let icon = NSImageView(image: NSWorkspace.shared.icon(forFile: item.path))
        icon.widthAnchor.constraint(equalToConstant: 32).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 32).isActive = true
        let title = text(url.lastPathComponent, size: 13, weight: .medium)
        let folder = text((url.deletingLastPathComponent().path as NSString).abbreviatingWithTildeInPath,
                          size: 11, secondary: true)
        let labels = NSStackView(views: [title, folder])
        labels.orientation = .vertical
        labels.alignment = .leading
        labels.spacing = 4
        labels.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let play = ActionButton("", symbol: "play.fill") { [weak self] in
            guard let self, !self.busy else { return }; self.onPlay?(item.path)
        }
        play.imagePosition = .imageOnly
        play.toolTip = "Play " + url.lastPathComponent
        play.setAccessibilityLabel("Play " + url.lastPathComponent)
        let view = NSStackView(views: [icon, labels, play])
        view.distribution = .fill
        view.edgeInsets = NSEdgeInsets(top: 8, left: 8, bottom: 8, right: 8)
        view.spacing = 12
        view.setAccessibilityLabel(url.lastPathComponent)
        return view
    }

    private var selected: String? {
        let row = table.clickedRow >= 0 ? table.clickedRow : table.selectedRow
        return items.indices.contains(row) ? items[row].path : nil
    }
    @objc private func playSelected() {
        if !busy && items.indices.contains(table.selectedRow) { onPlay?(items[table.selectedRow].path) }
    }
    @objc private func playContextSelection() { if !busy, let selected { onPlay?(selected) } }
    @objc private func revealSelected() {
        if let selected { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: selected)]) }
    }
    @objc private func removeSelected() { if let selected { onRemove?(selected) } }
}
