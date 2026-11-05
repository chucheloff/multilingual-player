class Bubbatv < Formula
  desc "Dual-language media player for macOS"
  homepage "https://github.com/chucheloff/multilingual-player"
  # Pin the app source; the release tag also includes this formula's metadata.
  url "https://github.com/chucheloff/multilingual-player.git",
      revision: "265a9c42367bbe4e0403c71f15ece957e2b769d8"
  version "0.1.0"
  # This project does not declare an SPDX license.
  license :cannot_represent

  depends_on macos: :sonoma
  depends_on "mpv"

  def install
    ENV["BUBBATV_DISTRIBUTION"] = "homebrew"
    ENV["BUBBATV_HOMEBREW_FORMULA"] = full_name
    ENV["BUBBATV_RUNTIME_PATH"] = "#{formula_opt_bin("mpv")}:/usr/bin:/bin:/usr/sbin:/sbin"
    system "tools/make-app.sh", "--standalone", prefix/"bubbaTV.app"

    bin.install_symlink prefix/"bubbaTV.app/Contents/Resources/Player/dualplay"
    (bin/"bubbatv").write <<~SH
      #!/bin/bash
      case "${1:-}" in
        --version) exec "#{opt_prefix}/bubbaTV.app/Contents/MacOS/bubbaTV" --version ;;
        -h|--help) printf '%s\\n' 'Usage: bubbatv [FILE_OR_FOLDER...]' 'Open the home window or play media.'; exit 0 ;;
      esac
      exec /usr/bin/open -a "#{opt_prefix}/bubbaTV.app" "$@"
    SH
    chmod 0755, bin/"bubbatv"
  end

  post_install_steps do
    # Homebrew relocates and re-signs the mpv plugin after the build. Seal the
    # outer app again so its resource hashes match the installed plugin.
    run "/usr/bin/codesign", args: ["--force", "--sign", "-", "{{prefix}}/bubbaTV.app"]
  end

  def caveats
    <<~EOS
      Start the home window with:
        bubbatv
      Or play media with:
        bubbatv movie.mkv

      To add a shortcut alongside a development app in Applications:
        ln -s #{opt_prefix}/bubbaTV.app ~/Applications/bubbaTV-homebrew.app
    EOS
  end

  test do
    app = prefix/"bubbaTV.app"
    player = app/"Contents/Resources/Player/dualplay"
    assert_equal "bubbaTV #{version}\n", shell_output("#{bin}/bubbatv --version")
    assert_path_exists app/"Contents/Resources/Player/tools/.build/audio-pairs"
    assert_path_exists app/"Contents/Resources/Player/tools/.build/bubbatv.so"
    distribution = shell_output("/usr/bin/plutil -extract BubbaTVDistribution raw -o - #{app}/Contents/Info.plist")
    assert_equal "homebrew", distribution.strip
    ENV["MPV_HOME"] = (testpath/"profile").to_s
    (testpath/"movie.mkv").write ""
    assert_match player.to_s, shell_output("#{app}/Contents/MacOS/bubbaTV --dry-run #{testpath}/movie.mkv")
    system bin/"dualplay", "av://lavfi:color=c=black:s=16x16:d=0.1",
           "--no-config", "--vo=null", "--ao=null", "--frames=1", "--really-quiet"
    system "/usr/bin/codesign", "--verify", "--strict", app
  end
end
