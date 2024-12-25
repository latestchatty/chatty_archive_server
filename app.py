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
    # Recursive query for thread hierarchy
    thread_query = """
        WITH RECURSIVE thread AS (
            SELECT 
                p.id, p.parent_id, p.thread_id, p.author, p.date, p.body, 0 AS depth,
                ARRAY[p.id]::integer[] AS path
            FROM post p
            WHERE p.id = %s

            UNION ALL

            SELECT 
                p.id, p.parent_id, p.thread_id, p.author, p.date, p.body, t.depth + 1,
                t.path || p.id
            FROM post p
            JOIN thread t ON p.parent_id = t.id
            WHERE p.thread_id = t.thread_id
        )
        SELECT id, parent_id, author, date, body, depth, path
        FROM thread
        ORDER BY path;
    """

    # Query for LOL tags
    tags_query = """
        SELECT post_id, tag, count
        FROM post_lols
        WHERE post_id = ANY(%s);
    """

    # Execute thread query
    conn = get_db_connection()
    with conn.cursor() as cur:
        # Fetch thread hierarchy
        cur.execute(thread_query, (post_id,))
        rows = cur.fetchall()

        # Extract post IDs from results
        post_ids = [row[0] for row in rows]

        # Fetch tags for these posts
        cur.execute(tags_query, (post_ids,))
        tags = cur.fetchall()
    conn.close()

    # Organize tags into a dictionary by post ID
    tag_map = {}
    for post_id, tag, count in tags:
        if post_id not in tag_map:
            tag_map[post_id] = {}
        tag_map[post_id][tag] = count

    # Calculate brightness based on date
    dates = [row[3] for row in rows]
    oldest_date = min(dates)
    newest_date = max(dates)

    def calculate_brightness(date):
        if oldest_date == newest_date:
            return 10
        ratio = (date - oldest_date) / (newest_date - oldest_date)
        return 1 + int(ratio * 9)

    # Format rows for output
    sanitized_rows = []
    for i, row in enumerate(rows):
        sanitized_rows.append((
            row[0],  # Post ID
            row[1],  # Parent ID
            row[2],  # Author
            row[3],  # Date
            sanitize_html(row[4], remove_br=(i > 0)),  # Condensed previews strip <br>
            row[5],  # Depth
            calculate_brightness(row[3]),  # Brightness
            tag_map.get(row[0], {})  # Tags as a dictionary (default to empty)
        ))
    return sanitized_rows

def fetch_posts_by_username(username, page=1, page_size=100, sort_by=None):
    """Fetch paginated posts by username with optional tag-based sorting."""
    offset = (page - 1) * page_size

    # Base query with optional tag filtering
    query = f"""
        SELECT p.id, p.thread_id, p.date, p.body, p.author,
               COALESCE(jsonb_object_agg(l.tag, l.count) FILTER (WHERE l.tag IS NOT NULL), '{{}}') AS tags
        FROM post p
        LEFT JOIN post_lols l ON p.id = l.post_id
        WHERE p.author ILIKE %s
        GROUP BY p.id, p.thread_id, p.date, p.body, p.author
    """

    # Add sorting based on a tag if specified
    if sort_by:
        query += f"""
            ORDER BY COALESCE(MAX(CASE WHEN l.tag = %s THEN l.count ELSE 0 END), 0) DESC, p.date DESC
        """
    else:
        query += " ORDER BY p.date DESC"

    # Add pagination
    query += " LIMIT %s OFFSET %s;"

    # Count query for total rows
    count_query = """
        SELECT COUNT(*)
        FROM post
        WHERE author ILIKE %s;
    """

    conn = get_db_connection()
    with conn.cursor() as cur:
        # Fetch results
        if sort_by:
            cur.execute(query, (username, sort_by, page_size, offset))
        else:
            cur.execute(query, (username, page_size, offset))
        rows = cur.fetchall()

        # Fetch total count
        cur.execute(count_query, (username,))
        total_count = cur.fetchone()[0]

    conn.close()

    # Organize results with sanitized tags
    sanitized_rows = []
    for row in rows:
        tags = row[5]  # Tags as JSON
        sanitized_rows.append((
            row[0],  # Post ID
            row[1],  # Thread ID
            row[2],  # Date
            sanitize_html(row[3][:100], remove_br=True, remove_links=True) + ('...' if len(row[3]) > 100 else ''),  # Preview
            row[4],  # Author
            tags if tags else {}  # Tags dictionary
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
    page = int(request.args.get('page', 1))  # Default to page 1
    page_size = int(request.args.get('page_size', PAGE_SIZE))  # Use the constant for default
    sort_by = request.args.get('sort_by', None)

    if not username:
        return redirect(url_for('home'))

    # Fetch posts and total count
    results, total_count = fetch_posts_by_username(username, page, page_size, sort_by)

    # Calculate total pages
    total_pages = (total_count // page_size) + (1 if total_count % page_size > 0 else 0)

    # Render template
    return render_template(
        'search.html',
        username=username,
        results=results,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        sort_by=sort_by,
        total_count=total_count
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