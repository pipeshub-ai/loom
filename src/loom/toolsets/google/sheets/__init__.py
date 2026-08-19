"""Google Sheets toolset — read and write cell values in a spreadsheet.

Rides the shared ``toolsets/google`` auth layer, so one cached token serves
Sheets alongside Gmail, Calendar, Drive and Meet. A separately-grantable
toolset for the reason the other four are: a workflow appending to a tracking
sheet has no business holding a mail-send scope.
"""
