---
layout: page
title: Categories
permalink: /categories/
---

<div>
  This is list of post sorted by tags. One post can have several tags and so it could appear in several categories!
</div>

<div id="tags-list">
{% for tag in site.tags %}
  {% assign tag_name = tag | first %}
  {% assign tag_name_pretty = tag_name | replace: "_", " " | capitalize %}
  <div class="tag-list">
    <div id="#{{ tag_name | slugize }}"></div>
    <h3 class="post-list-heading line-bottom"> In #{{ tag_name }}: </h3>
    <a name="{{ tag_name | slugize }}"></a>
    <ul class="post-list post-list-narrow">
     {% for post in site.tags[tag_name] %}
     <li>
       {%- assign date_format = site.minima.date_format | default: "%b %-d, %Y" -%}
       <b>
         <a href="{{ post.url | relative_url }}">
           {{ post.title | escape }}
         </a>
       </b> - <i>{{ post.date | date: date_format }}</i>
     </li>
     {% endfor %}
    </ul>
  </div>
{% endfor %}
</div>