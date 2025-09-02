import subprocess
import time
import random
from os.path import abspath, dirname, join
import configparser
import json
from datetime import datetime
import requests
from discord_webhook import DiscordWebhook, DiscordEmbed

# --- File paths ---
last_change_path = join(dirname(abspath(__file__)), "last_change.ini")
config_path = join(dirname(abspath(__file__)), "config.ini")
sig_path = join(dirname(abspath(__file__)), "bot_signatures.json")
emoji_path = join(dirname(abspath(__file__)), "emojis.json")
review_messages_path = join(dirname(abspath(__file__)), "review_messages.json")

# --- Utilities ---
signatures = json.load(open(sig_path, 'r', encoding='utf-8'))
def get_funny_signature():
    return signatures[random.randint(0, len(signatures)-1)]

emojis = json.load(open(emoji_path, 'r', encoding='utf-8'))
def get_random_emoji():
    return emojis[random.randint(0, len(emojis)-1)]

review_messages = json.load(open(review_messages_path, 'r', encoding='utf-8'))
def save_review_messages():
        with open(review_messages_path, "w", encoding="utf-8") as f:
            json.dump(review_messages, f, indent=2)

# ----------------------------------------------------------------------
class Change():
    def __init__(self, change_header, content, depots=None, swarm_lookup=None, swarm_url=None):
        split = change_header.split(" ")
        self.num = split[1]
        self.date = split[3]
        self.time = split[4]
        self.user = split[6]
        self.content = content
        self.depots = depots or []
        self.username = self.user
        self.workspace = "unknown"
        self.reviewId = None
        self.reviewLink = None
        self.swarm_url = swarm_url

        # Extract username and workspace separately
        if "@" in self.user:
            parts = self.user.split("@", 1)
            self.username = parts[0]
            raw_workspace = parts[1]

            # Simplify swarm workspaces
            if raw_workspace.startswith("swarm-") and swarm_lookup:
                self.workspace = "swarm"
                review_info = swarm_lookup(self.num)
                if review_info:
                    self.reviewId = review_info.get("reviewId")
                    self.reviewLink = review_info.get("reviewLink")
            else:
                self.workspace = raw_workspace

        # Convert to datetime object (safe parse)
        try:
            self.dt = datetime.strptime(f"{self.date} {self.time}", "%Y/%m/%d %H:%M:%S")
        except ValueError:
            self.dt = None


# ----------------------------------------------------------------------
class PerforceLogger():
    def __init__(self, submission_url, repository, review_url=None, swarm_url=None, swarm_user=None, swarm_ticket=None, engineer_role_id=None):
        self.submission_url = submission_url
        self.repository = repository
        self.review_url = review_url
        self.swarm_url = swarm_url
        self.swarm_user = swarm_user
        self.swarm_ticket = swarm_ticket
        self.engineer_role_id = engineer_role_id

        # load persisted review→message map
        try:
            with open(review_messages_path, "r", encoding="utf-8") as f:
                review_messages = json.load(f)
        except FileNotFoundError:
            review_messages = {}

    # ---------------- P4 stuff ----------------
    def p4_fetch(self, max):
        p4_changes = subprocess.Popen(
            f'p4 changes -t -m {max} -s submitted -e {self.read_num()+1} -l {self.repository}',
            stdout=subprocess.PIPE, shell=True)
        return p4_changes.stdout.read().decode('ISO-8859-1')

    def get_depots_for_change(self, changenum):
        p = subprocess.Popen(f"p4 describe -s {changenum}", stdout=subprocess.PIPE, shell=True)
        output = p.stdout.read().decode("ISO-8859-1")
        depots = set()
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("... //"):
                parts = line.split("/")
                if len(parts) > 2:
                    depot_root = parts[2]
                    depots.add(depot_root)
        return list(depots)

    # ---------------- Swarm API ----------------
    def get_swarm_review_info(self, changenum):
        if not self.swarm_url:
            return {}
        base_url = self.swarm_url.rstrip("/")
        url = f"{base_url}/api/v11/reviews"
        auth = (self.swarm_user, self.swarm_ticket) if self.swarm_user and self.swarm_ticket else None
        try:
            resp = requests.get(url, auth=auth, timeout=5)
            resp.raise_for_status()
            data = resp.json().get('data')
            for review in data.get("reviews", []):
                commits = review.get("commits", [])
                if int(changenum) in commits:
                    return {
                        "reviewId": review.get("id"),
                        "reviewLink": f"{self.swarm_url}reviews/{review.get('id')}"
                    }
            return {}
        except Exception as e:
            print(f"Swarm API lookup failed for Change #{changenum}: {e}")
            return {}

    # ---------------- Submissions ----------------
    def regroup_changes(self, output):
        changes = []
        if len(output) > 0:
            last_num_str = ""
            lines = output.splitlines()
            str_header = ""
            str_content_buffer = []
            for l in lines:
                if l.startswith('Change'):
                    if len(str_content_buffer) > 0:
                        changenum = str_header.split(" ")[1]
                        depots = self.get_depots_for_change(changenum)
                        changes.append(Change(str_header, ''.join(str_content_buffer),
                                              depots=depots, swarm_lookup=self.get_swarm_review_info,
                                              swarm_url=self.swarm_url))
                    else:
                        last_num_str = l.split(" ")[1]
                    str_header = l
                    str_content_buffer = []
                else:
                    str_content_buffer.append(l + "\n")
            changenum = str_header.split(" ")[1]
            depots = self.get_depots_for_change(changenum)
            changes.append(Change(str_header, ''.join(str_content_buffer),
                                  depots=depots, swarm_lookup=self.get_swarm_review_info,
                                  swarm_url=self.swarm_url))
            if last_num_str != "":
                self.save_num(int(last_num_str))
        return changes

    def save_num(self, number):
        with open(last_change_path, 'w') as f:
            f.write('%d' % number)

    def read_num(self):
        try:
            with open(last_change_path, 'r') as f:
                return int(f.read())
        except:
            return 0

    def handle_new_submissions(self, signature=False):
        changes_as_str = self.p4_fetch(max=50)
        changes = self.regroup_changes(changes_as_str)
        for payload in reversed(changes):
            if payload != '':
                user = payload.user.split("@")[0]
                emoji = get_random_emoji()
                emoji2 = get_random_emoji()
                depots_str = ", ".join(payload.depots) if payload.depots else "unknown depot"
                if payload.dt:
                    formatted_time = payload.dt.strftime("%a %m/%d/%y, %-I:%M%p")
                else:
                    formatted_time = f"{payload.date} {payload.time}"
                workspace_str = f"**Workspace:** `{payload.workspace}`"
                if payload.workspace == "swarm" and payload.reviewLink:
                    workspace_str += f" ([Review #{payload.reviewId}]({payload.reviewLink}))"

                webhook = DiscordWebhook(url=self.submission_url)

                # create embed object for webhook
                embed = DiscordEmbed(
                    title="NEW SUBMISSION!",
                    description=(
                        f"`{user}` submitted to `{depots_str}` {emoji}\n"
                        f"{workspace_str}\n\n"
                        f"{emoji2} `Change #{payload.num}` on {formatted_time} {emoji2}\n"
                        f"```fix\n{payload.content.lstrip()}``` "
                    ),
                    color="0x51D1EC")
                webhook.add_embed(embed)

                if signature:
                    sig = get_funny_signature()
                    embed.set_footer(text=f"{sig}", ts=True)

                webhook.execute()
                print(f"Created new post for Change #{payload.num} in submissions webhook.")
            time.sleep(1)

    # ---------------- New Reviews ----------------
    def filter_participants(self, participants, author):
        """Exclude the review author from the participant list."""
        return [p for p in participants if p != author]
    
    def format_participants(self, participants, participants_data) -> str:
        """Format participants based on their vote state."""

        lines = ["**Participants Status:**"]
        for user in participants:
            vote = None
            if participants_data:
                vote = participants_data.get(user, {}).get("vote", {}).get("value")

            if vote == 1:
                status = SWARM_UP_VOTE_EMOJI
            elif vote == -1:
                status = SWARM_DOWN_VOTE_EMOJI
            else:
                status = SWARM_NO_VOTE_EMOJI
            lines.append(f"{status} `{user}`")

        return "\n".join(lines) if len(lines) > 1 else "No participants yet."
    
    def review_color(self, state: str) -> int:
        state_colors = {
            "needsReview": 0x3498DB,    # blue
            "needsRevision": 0xF39C12,  # yellow-orange
            "approved": 0x2ECC71,       # green
            "rejected": 0xE74C3C,       # red
            "archived": 0xBDC3C7        # light gray
        }
        return state_colors.get(state, 0x95A5A6)  # fallback: grey


    def handle_new_reviews(self):
        """Fetches reviews from P4 Code Review (Helix Swarm). Posts to Discord about any new ones and updates statuses of existing ones.
        Only updates Discord embeds if the review’s state or participants have changed.
        """
        if not self.swarm_url or not self.review_url:
            return

        base_url = self.swarm_url.rstrip("/")
        url = f"{base_url}/api/v11/reviews"
        auth = (self.swarm_user, self.swarm_ticket) if self.swarm_user and self.swarm_ticket else None

        try:
            resp = requests.get(url, auth=auth, timeout=5)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            reviews = data.get("reviews", [])

            for review in reversed(reviews):  # oldest -> newest
                reviewId = str(review["id"])
                state = review.get("state", "needsReview")
                embed_color = self.review_color(state)
                desc = review.get("description", "").strip() or "(no description)"
                link = f"{self.swarm_url}reviews/{reviewId}"
                author = review.get("author", "unknown")

                # Handle projects safely
                projects_field = review.get("projects")
                if isinstance(projects_field, dict) and projects_field:
                    project_list = []
                    for project_name, inner in projects_field.items():
                        if isinstance(inner, dict):
                            depots = list(inner.values())
                        elif isinstance(inner, list):
                            depots = inner
                        else:
                            depots = []
                        if depots:
                            project_list.append(f"{project_name} ({', '.join(depots)})")
                        else:
                            project_list.append(project_name)
                    project = ", ".join(project_list)
                else:
                    project = "unknown-project"

                # Participants & votes
                participants = review.get("participants", [])
                participants_data = review.get("participantsData", {})
                participants_filtered = self.filter_participants(participants, author)
                participants_text = self.format_participants(participants_filtered, participants_data)

                role_mention = f"<@&{self.engineer_role_id}>" if self.engineer_role_id else "@Engineer"

                # Determine if we need to post/edit
                need_update = False
                stored_info = review_messages.get(reviewId)
                if not stored_info:
                    # New review
                    need_update = True
                    message_id = None
                else:
                    message_id = stored_info.get("message_id")
                    last_state = stored_info.get("last_state")
                    last_participants_text = stored_info.get("last_participants_text")
                    if last_state != state or last_participants_text != participants_text:
                        need_update = True
                    

                if need_update:
                    if not message_id:
                        # --- New review ---
                        webhook = DiscordWebhook(url=self.review_url, content=role_mention)
                        embed = DiscordEmbed(
                            title=f"Review #{reviewId}: `{state}`",
                            description=(
                                f"`{author}` requested a review in `{project}`!\n\n"
                                f"Vote on the review here [Review #{reviewId}]({link}).\n"
                                f"```md\n{desc}\n```\n"
                                f"{participants_text}"
                            ),
                            color=embed_color
                        )
                        webhook.add_embed(embed)
                        response = webhook.execute()
                        print(f"Created new post for Review #{reviewId} in reviews webhook.")
                        if hasattr(response, "json"):
                            data = response.json()
                            message_id = data.get("id")
                    else:
                        # --- Existing review ---
                        webhook = DiscordWebhook(
                            url=self.review_url,
                            id=message_id,
                            content=role_mention
                        )
                        embed = DiscordEmbed(
                            title=f"Review #{reviewId}: `{state}`",
                            description=(
                                f"`{author}` requested a review in `{project}`!\n\n"
                                f"Vote on the review here [Review #{reviewId}]({link}).\n"
                                f"```md\n{desc}\n```\n"
                                f"{participants_text}"
                            ),
                            color=embed_color
                        )
                        webhook.add_embed(embed)
                        webhook.edit()
                        print(f"Updated post for Review #{reviewId} in reviews webhook.")

                    # Update stored info
                    review_messages[reviewId] = {
                        "message_id": message_id,
                        "last_state": state,
                        "last_participants_text": participants_text
                    }
                    save_review_messages()
                    time.sleep(1)
        except Exception as e:
            print(f"Error fetching/updating reviews: {e}")

# ----------------------------------------------------------------------
if __name__ == "__main__":
    config = configparser.ConfigParser()
    config.read(config_path)
    
    SUBMISSIONS_WEBHOOK_URL = config['Discord'].get('submissions_webhook', fallback=None)
    REVIEWS_WEBHOOK_URL = config['Discord'].get('reviews_webhook', fallback=None)
    ENGINEER_ROLE_ID = config['Discord'].get('engineer_role_id', fallback=None)

    P4_TARGET = config['Perforce'].get('target', '')
    MAX_CHANGES = config.getint('ApplicationSettings', 'max_changes', fallback=50)
    ALLOW_SIGNATURE = config.getboolean('ApplicationSettings', 'enable_signature', fallback=True)

    SWARM_URL = config['Swarm'].get('url', fallback=None)
    SWARM_USER = config['Swarm'].get('user', fallback=None)
    SWARM_TICKET = config['Swarm'].get('ticket', fallback=None)
    SWARM_UP_VOTE_EMOJI = config['Swarm'].get('up-vote-emoji', fallback='[ UP ]')
    SWARM_DOWN_VOTE_EMOJI = config['Swarm'].get('down-vote-emoji', fallback='[DOWN]')
    SWARM_NO_VOTE_EMOJI = config['Swarm'].get('no-vote-emoji', fallback='[NONE]')

    logger = PerforceLogger(
        SUBMISSIONS_WEBHOOK_URL,
        repository=P4_TARGET,
        swarm_url=SWARM_URL,
        swarm_user=SWARM_USER,
        swarm_ticket=SWARM_TICKET,
        engineer_role_id=ENGINEER_ROLE_ID
    )

     # Always check for submissions
    logger.handle_new_submissions(signature=ALLOW_SIGNATURE)

    # Only check for reviews if a webhook is supplied
    if REVIEWS_WEBHOOK_URL:
        logger.review_url = REVIEWS_WEBHOOK_URL
        logger.handle_new_reviews()
