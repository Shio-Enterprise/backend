from django.dispatch import Signal

# Sent after any successful sign-in that returns tokens (password, sign-up or Google).
# Receivers should accept: sender, request, user
login_completed = Signal()

# Kept for compatibility: Google login used to have its own signal.
google_login_completed = login_completed
