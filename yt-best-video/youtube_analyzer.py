from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from config import YOUTUBE_API_KEY


def analizar_rendimiento_canal(canal_id, nombre_canal):
    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

        fecha_limite = datetime.now(timezone.utc) - timedelta(days=15)
        fecha_iso = fecha_limite.strftime("%Y-%m-%dT%H:%M:%SZ")

        search_response = (
            youtube.search()
            .list(
                channelId=canal_id,
                part="id",
                type="video",
                order="date",
                publishedAfter=fecha_iso,
                maxResults=50,
            )
            .execute()
        )

        video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]

        if not video_ids:
            return None

        videos_response = (
            youtube.videos()
            .list(
                part="snippet,statistics",
                id=",".join(video_ids),
            )
            .execute()
        )

        videos = []
        for item in videos_response.get("items", []):
            stats = item.get("statistics", {})
            views = int(stats.get("viewCount", 0))
            titulo = item["snippet"]["title"]
            videos.append({"id": item["id"], "title": titulo, "views": views})

        if not videos:
            return None

        promedio_views = sum(v["views"] for v in videos) / len(videos)

        ganadores = [v for v in videos if v["views"] > promedio_views]

        if not ganadores:
            ganadores = videos

        mejor = max(ganadores, key=lambda x: x["views"])
        return {
            "title": mejor["title"],
            "views": mejor["views"],
            "ch_name": nombre_canal,
            "video_id": mejor["id"],
        }

    except HttpError as e:
        print(f"[ERROR] API error para canal {nombre_canal}: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Error inesperado para canal {nombre_canal}: {e}")
        return None
