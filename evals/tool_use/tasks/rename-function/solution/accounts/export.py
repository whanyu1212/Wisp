import csv
import io

from accounts.names import display_name as name_of


def to_csv(users):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for user in users:
        writer.writerow([name_of(user), user["email"]])
    return buffer.getvalue()
