"""Telegram transport: the Bot API client, the owner gate, polling and the outbox.

Import the submodules directly (bot, outbox, gate, poll, commands). This
package re-exports nothing on purpose: gate depends on the safety module, and
importing bot or outbox should not pull that in.
"""
