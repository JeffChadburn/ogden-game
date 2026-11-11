// Starter Unity chunk streamer for the Ogden environment export.
// Reads manifest.json, loads/unloads GLB chunks by planar distance.
//
// This script has NOT been play-tested in the Unity editor. It is a documented
// starting point. It expects:
//   - GLTFast (https://docs.unity3d.com/Packages/com.unity.cloud.gltfast)
//   - Optional: Addressables (if useAddressables is true)
//   - manifest.json copied into StreamingAssets or assigned as a TextAsset
//
// Game axes in the export: X = east, Y = up, Z = north, meters, origin at
// origin_utm12n_sw. Place this component on a manager object. Assign the
// player transform (or it will use Camera.main).

using System;
using System.Collections.Generic;
using System.IO;
using System.Threading.Tasks;
using UnityEngine;
#if GLTFAST
using GLTFast;
#endif

[Serializable]
public class OgdenManifest
{
    public Origin origin_utm12n_sw;
    public float chunk_size_m;
    public LodDistances lod_distances_m;
    public List<FeatureChunk> feature_chunks;
    public List<TerrainChunk> terrain_chunks;
}

[Serializable] public class Origin { public double easting; public double northing; }
[Serializable] public class LodDistances { public float lod0 = 350, lod1 = 700, lod2 = 1400, unload = 1800; }

[Serializable]
public class FeatureChunk
{
    public string id;
    public BoundsGame bounds_game;
    // files is a dictionary in JSON; Unity's default JsonUtility cannot parse
    // arbitrary keys. Use a tiny JSON library (Newtonsoft) or flatten the
    // manifest. See LoadFilesFallback below.
}

[Serializable]
public class TerrainChunk
{
    public string id;
    public BoundsGame bounds_game;
    public List<LodFile> lods;
}

[Serializable] public class LodFile { public int lod; public string file; public float spacing_m; }
[Serializable] public class BoundsGame { public float minx, minz, maxx, maxz; }

public class ChunkStreamer : MonoBehaviour
{
    [Tooltip("manifest.json as a TextAsset (or leave empty and set streamingAssetsRelativePath).")]
    public TextAsset manifestText;
    public string streamingAssetsRelativePath = "ogden/manifest.json";
    public Transform player;
    public string contentRoot = "ogden"; // folder containing roads/, buildings/, terrain/chunks/
    public bool useAddressables = false;
    public float loadPadding = 80f;

    OgdenManifest manifest;
    readonly Dictionary<string, GameObject> loaded = new Dictionary<string, GameObject>();
    readonly HashSet<string> loading = new HashSet<string>();

    async void Start()
    {
        if (player == null && Camera.main != null)
            player = Camera.main.transform;

        string json = manifestText != null
            ? manifestText.text
            : File.ReadAllText(Path.Combine(Application.streamingAssetsPath, streamingAssetsRelativePath));

        // JsonUtility cannot read the "files" dictionary on feature chunks.
        // Prefer Newtonsoft.Json if present; otherwise this still parses lods
        // on terrain chunks and you can extend FeatureChunk as needed.
        manifest = JsonUtility.FromJson<OgdenManifest>(json);
        if (manifest == null)
        {
            Debug.LogError("ChunkStreamer: failed to parse manifest.json");
            return;
        }
        await Task.Yield();
    }

    void Update()
    {
        if (manifest == null || player == null) return;
        Vector3 p = player.position;
        float loadR = (manifest.lod_distances_m != null ? manifest.lod_distances_m.lod0 : 350f) + loadPadding;
        float unloadR = manifest.lod_distances_m != null ? manifest.lod_distances_m.unload : 1800f;

        if (manifest.terrain_chunks != null)
        {
            foreach (var ch in manifest.terrain_chunks)
                Consider(ch.id, ch.bounds_game, p, loadR, unloadR, () => LoadTerrain(ch));
        }
        // Feature chunks: hook up after you deserialize the files map.
    }

    void Consider(string id, BoundsGame b, Vector3 p, float loadR, float unloadR, Action load)
    {
        if (b == null) return;
        float cx = (b.minx + b.maxx) * 0.5f;
        float cz = (b.minz + b.maxz) * 0.5f;
        float dx = p.x - cx, dz = p.z - cz;
        float d = Mathf.Sqrt(dx * dx + dz * dz);
        if (d <= loadR && !loaded.ContainsKey(id) && !loading.Contains(id))
            load();
        else if (d > unloadR && loaded.TryGetValue(id, out var go))
        {
            Destroy(go);
            loaded.Remove(id);
        }
    }

    async void LoadTerrain(TerrainChunk ch)
    {
        if (ch.lods == null || ch.lods.Count == 0) return;
        loading.Add(ch.id);
        // Pick lod0 for now; swap by distance if you want.
        string file = ch.lods[0].file;
        string path = Path.Combine(contentRoot, "terrain", "chunks", file);
        GameObject root = new GameObject(ch.id);
#if GLTFAST
        var gltf = new GltfImport();
        bool ok = useAddressables
            ? await gltf.Load(path) // replace with Addressables.LoadAssetAsync<TextAsset> in your project
            : await gltf.Load(Path.Combine(Application.streamingAssetsPath, path));
        if (ok) await gltf.InstantiateMainSceneAsync(root.transform);
#else
        Debug.LogWarning("ChunkStreamer: define GLTFAST and install glTFast to load " + path);
#endif
        loaded[ch.id] = root;
        loading.Remove(ch.id);
    }
}
