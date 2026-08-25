# _plugins/audio_enclosure.rb
#
# Two Liquid filters used by feed.xml to build valid <enclosure> tags:
#
#   {{ post.audio | audio_file_size }}   ->  bytes, required by the RSS spec
#   {{ post.audio | audio_duration }}    ->  H:MM:SS, for <itunes:duration>
#
# `post.audio` is a site-root-relative path like "/assets/audio/eclipse.mp3",
# matching what narrate.py writes and what _includes/audio.html reads.

require "shellwords"

module AudioEnclosure
  def audio_file_size(path)
    return 0 if path.nil? || path.empty?
    full = File.join(@context.registers[:site].source, path.sub(%r{\A/}, ""))
    File.exist?(full) ? File.size(full) : 0
  end

  def audio_duration(path)
    return "0:00" if path.nil? || path.empty?
    full = File.join(@context.registers[:site].source, path.sub(%r{\A/}, ""))
    return "0:00" unless File.exist?(full)

    # ffprobe is optional. Without it, itunes:duration is omitted upstream by
    # not having a real value - podcast apps compute duration themselves from
    # the audio file, so this is a nicety rather than a requirement.
    seconds = probe_seconds(full)
    return "0:00" unless seconds

    h, rem = seconds.divmod(3600)
    m, s = rem.divmod(60)
    h.positive? ? format("%d:%02d:%02d", h, m, s) : format("%d:%02d", m, s)
  end

  private

  def probe_seconds(path)
    cmd = "ffprobe -v error -show_entries format=duration " \
          "-of default=noprint_wrappers=1:nokey=1 #{Shellwords.escape(path)}"
    out = `#{cmd}`.strip
    return nil if out.empty?
    out.to_f.round
  rescue StandardError
    nil
  end
end

Liquid::Template.register_filter(AudioEnclosure)
