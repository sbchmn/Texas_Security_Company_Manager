from django.core.management.base import BaseCommand

from core.services import close_stale_hold_overs, expire_stale_moves


class Command(BaseCommand):
    help="Close shift offers and trades whose post started before anyone answered, and tell both officers."

    def handle(self, *args, **options):
        # The same concern covers a hold-over left open at 22:15: a move nobody answered is closed by
        # the post starting, and a hold-over nobody closed is answered by the officer's own
        # clock-out. Both are facts the system can read rather than reminders it has to send.
        self.stdout.write(f"expired={expire_stale_moves()} hold_overs_closed={close_stale_hold_overs()}")
