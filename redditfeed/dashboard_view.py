"""Jinja template for the RedditFeed dashboard page. No discord/redbot imports.

The pause/resume form is rendered by the dashboard itself (it hands the template a
pre-rendered `form`, so it is output with `|safe`, never built field by field).

All data reaches the template as variables (never formatted into this string),
and every value is passed through `|e` -- that is safe whether or not the
dashboard's Jinja environment has autoescape on (an already-escaped value is
not escaped twice).
"""

PAGE_TEMPLATE = """\
<div class="redditfeed-page">
  <h3>RedditFeed</h3>
  <p class="text-muted">Subreddit feeds posting into {{ guild_name|e }}.</p>

  {% if rows %}
  <div class="table-responsive">
    <table class="table table-sm align-middle">
      <thead>
        <tr>
          <th>Subreddit</th><th>State</th><th>Channels</th>
          <th>Last poll</th><th>Last post found</th><th>Filters</th><th>Last error</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td>r/{{ r.subreddit|e }}</td>
          <td>{{ r.state|e }}</td>
          <td>{{ r.channels|join(", ")|e }}</td>
          <td>{{ r.last_poll|e }}</td>
          <td>{{ r.last_post|e }}</td>
          <td>
            {% if r.require %}require: {{ r.require|e }}<br>{% endif %}
            {% if r.block %}block: {{ r.block|e }}{% endif %}
            {% if not r.require and not r.block %}none{% endif %}
          </td>
          <td>{{ r.last_error|e }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p>No subreddits are mapped to channels in this server yet. Use <code>.redditfeed add &lt;subreddit&gt; #channel</code>.</p>
  {% endif %}

  {% if form %}
  {{ form|safe }}
  {% elif rows and not can_edit %}
  <p class="text-muted">Pausing or resuming a feed needs the mod role or Manage Server.</p>
  {% endif %}
</div>
"""
