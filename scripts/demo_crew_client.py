"""Call the mock crew-roster API through the crew plugin client.

Start the API first:  make up
Then run:             make demo
"""

import asyncio
import logging

from crewgate.plugins.crew.client import RosterClient


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    async with RosterClient() as client:
        crew = [c async for c in client.iter_crew(page_size=25)]
        print(f"Fetched {len(crew)} crew members across pages")
        roster = await client.get_roster(crew[0]["crew_id"])
        flights = [d for d in roster["days"] if d["duty"] == "FLT"]
        print(f"{crew[0]['crew_id']} has {len(flights)} flight duties in the next 14 days")
        swap = await client.request_swap(crew[0]["crew_id"], crew[1]["crew_id"], roster["days"][2]["date"])
        print(f"Swap {swap['swap_id']} created with status {swap['status']}")
        print(f"Token fetches: {client.auth.fetch_count}, retries: {len(client.partner.retry_log)}")


if __name__ == "__main__":
    asyncio.run(main())
