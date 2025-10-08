# generate_tags.rb
require 'fileutils'
require 'yaml'

POSTS_DIR = '_posts'
TAGS_DIR = 'tags'

Dir.mkdir(TAGS_DIR) unless Dir.exist?(TAGS_DIR)

all_tags = []

Dir.glob("#{POSTS_DIR}/*.{md,markdown}") do |post_path|
  content = File.read(post_path)

  # Extract YAML front matter safely
  if content =~ /\A---\s*\n(.*?)\n---/m
    begin
      front_matter = YAML.load($1) || {}
    rescue Psych::SyntaxError => e
      puts "⚠️  Skipping #{post_path} due to YAML error: #{e.message}"
      next
    end

    tags = front_matter['tags']
    next unless tags

    # Normalize tags into an array
    tags = tags.is_a?(String) ? tags.split(/\s+/) : tags
    all_tags.concat(tags)
  end
end

all_tags.uniq!

if all_tags.empty?
  puts "⚠️  No tags found in #{POSTS_DIR}/"
  exit
end

# Load tag descriptions
tag_descriptions = YAML.load_file('tag_descriptions.yml')

# Generate a page per tag
all_tags.each do |tag|
  # Clean up slug for URLs
  slug = tag.downcase.strip.gsub(/[^\w]+/, '-')
  tag_dir = File.join(TAGS_DIR, slug)
  description = tag_descriptions[tag] || " "
  FileUtils.mkdir_p(tag_dir)

  File.write(
    File.join(tag_dir, 'index.md'),
    <<~HEREDOC
    ---
    layout: tag
    tag: #{tag}
    description: #{description}
    permalink: /tags/#{slug}/
    description: "#{description}"
    ---
    HEREDOC
  )
end

puts "✅ Generated tag pages for: #{all_tags.join(', ')}"