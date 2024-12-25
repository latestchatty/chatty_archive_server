from flask import Flask, render_template, request, jsonify
import psycopg2
import bleach
import os
from config import DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT
from html.parser import HTMLParser

# Constants
PAGE_SIZE = 50  # Default page size

app = Flask(__name__)

# Allowed HTML tags and attributes
ALLOWED_TAGS = [
    'br', 'b', 'i', 'u', 'em', 'strong', 'a', 'br', 'p', 'ul', 'ol', 'li', 'blockquote', 'code', 'pre'
]
ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title'],
    'img': ['src', 'alt', 'title', 'width', 'height'],
    'code': ['class']
}

class TagStripper(HTMLParser):
    """Custom HTML Parser to strip specific tags like <br>."""
    def __init__(self, remove_tags):
        super().__init__()
        self.remove_tags = remove_tags
        self.result = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.remove_tags:
            self.result.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag not in self.remove_tags:
            self.result.append(f"</{tag}>")

    def handle_data(self, data):
        self.result.append(data)

    def get_data(self):
        return ''.join(self.result)


def strip_tags(html, remove_tags):
    """Remove specific tags while keeping others intact."""
    stripper = TagStripper(remove_tags)
    stripper.feed(html)
    return stripper.get_data()


def sanitize_html(html, remove_br=False, remove_links=False):
    """Sanitize HTML and optionally strip <br> tags and links."""
    # Clean HTML with bleach
    cleaned_html = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES)

    # Remove specific tags (e.g., <br>)
    if remove_br:
        cleaned_html = strip_tags(cleaned_html, remove_tags=['br'])

    # Remove links if required
    if remove_links:
        cleaned_html = bleach.clean(cleaned_html, tags=[], strip=True)

    return cleaned_html

def fetch_thread(post_id):
    query = """
        WITH RECURSIVE thread AS (
            SELECT 
                p.id, p.parent_id, p.thread_id, p.author, p.date, p.body, 0 AS depth,
                ARRAY[p.id]::integer[] AS path,
                COALESCE(l.count, 0) AS lol_count -- Join for LOL count
            FROM post p
            LEFT JOIN post_lols l ON p.id = l.post_id AND l.tag = 'lol' -- Filter for 'lol' tags
            WHERE p.id = %s

            UNION ALL

            SELECT 
                p.id, p.parent_id, p.thread_id, p.author, p.date, p.body, t.depth + 1,
                t.path || p.id,
                COALESCE(l.count, 0) AS lol_count -- Include LOL count for children
            FROM post p
            JOIN thread t ON p.parent_id = t.id
            LEFT JOIN post_lols l ON p.id = l.post_id AND l.tag = 'lol'
            WHERE p.thread_id = t.thread_id
        )
        SELECT id, parent_id, author, date, body, depth, lol_count
        FROM thread
        ORDER BY path; -- Depth-first ordering
    """
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(query, (post_id,))
        rows = cur.fetchall()
    conn.close()

    # Calculate brightness based on date
    dates = [row[3] for row in rows]
    oldest_date = min(dates)
    newest_date = max(dates)

    def calculate_brightness(date):
        if oldest_date == newest_date:
            return 10  # Single post, always brightest
        ratio = (date - oldest_date) / (newest_date - oldest_date)
        return 1 + int(ratio * 9)

    sanitized_rows = []
    for i, row in enumerate(rows):
        sanitized_rows.append((
            row[0],  # Post ID
            row[1],  # Parent ID
            row[2],  # Author
            row[3],  # Date
            sanitize_html(row[4], remove_br=(i > 0)),  # Strip <br> for previews
            row[5],  # Depth
            calculate_brightness(row[3]),  # Brightness level
            row[6]  # LOL count
        ))
    return sanitized_rows

def fetch_posts_by_username(username, page=1, page_size=PAGE_SIZE):
    """Fetch paginated posts by username."""
    offset = (page - 1) * page_size

    query = """
        SELECT id, thread_id, date, body
        FROM post
        WHERE author ILIKE %s
        ORDER BY date DESC
        LIMIT %s OFFSET %s;
    """
    count_query = """
        SELECT COUNT(*)
        FROM post
        WHERE author ILIKE %s;
    """
    conn = get_db_connection()
    with conn.cursor() as cur:
        # Fetch paginated results
        cur.execute(query, (username, page_size, offset))
        rows = cur.fetchall()

        # Fetch total post count
        cur.execute(count_query, (username,))
        total_count = cur.fetchone()[0]

    conn.close()

    # Sanitize and strip <br> for previews
    sanitized_rows = []
    for row in rows:
        sanitized_rows.append((
            row[0],  # id
            row[1],  # thread_id
            row[2],  # date
            sanitize_html(row[3][:100], remove_br=True, remove_links=True) + ('...' if len(row[3]) > 100 else '')  # preview
        ))

    return sanitized_rows, total_count

# Connect to Database
def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT
        )
        return conn
    except psycopg2.OperationalError as e:
        print("Database connection failed:", e)
        raise

# Landing Page
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/search')
def search():
    # Get query parameters
    username = request.args.get('by_user', '').strip()
    page = int(request.args.get('page', 1))

    if not username:
        # Redirect back to home if no username is provided
        return redirect(url_for('home'))

    # Fetch paginated results
    results, total_count = fetch_posts_by_username(username, page)

    return render_template(
        'search.html',
        username=username,
        results=results,
        total_count=total_count,
        page=page,
        page_size=PAGE_SIZE
    )


# Thread Route
@app.route('/thread/<int:post_id>')
def thread(post_id):
    try:
        rows = fetch_thread(post_id)
        return render_template('thread.html', rows=rows)
    except Exception as e:
        return f"Error fetching thread: {str(e)}", 500

if __name__ == '__main__':
    app.run(debug=True)