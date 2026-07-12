using System;
using System.Collections;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text.Json;

if (args.Length != 4)
{
    Console.Error.WriteLine("Usage: MajdataBridge <Majdata directory> <maidata.txt> <difficulty id> <output.json>");
    return 2;
}

var majdataDir = Path.GetFullPath(args[0]);
var maidataPath = Path.GetFullPath(args[1]);
var difficulty = int.Parse(args[2]);
var outputPath = Path.GetFullPath(args[3]);
if (difficulty is < 1 or > 7)
    throw new ArgumentOutOfRangeException(nameof(difficulty), "Difficulty must be between 1 and 7.");

var assemblyPath = Path.Combine(majdataDir, "MajdataEdit.dll");
if (!File.Exists(assemblyPath))
    throw new FileNotFoundException("MajdataEdit.dll was not found.", assemblyPath);

var assembly = Assembly.LoadFrom(assemblyPath);
var simaiType = RequireType(assembly, "MajdataEdit.SimaiProcess");
var majsonType = RequireType(assembly, "MajdataEdit.Majson");

InvokeStatic(simaiType, "ClearData");
var readOk = (bool)(InvokeStatic(simaiType, "ReadData", maidataPath) ?? false);
if (!readOk)
    throw new InvalidDataException($"MajdataEdit could not parse {maidataPath}.");

var charts = (string?[])RequireField(simaiType, "fumens").GetValue(null)!;
var chartText = charts[difficulty - 1];
if (string.IsNullOrWhiteSpace(chartText))
    throw new InvalidDataException($"Difficulty {difficulty} is empty.");

InvokeStatic(simaiType, "Serialize", chartText, 0L);
var sourceNotes = (IEnumerable)RequireField(simaiType, "notelist").GetValue(null)!;
var majson = Activator.CreateInstance(majsonType, nonPublic: true)
             ?? throw new InvalidOperationException("Could not create Majson.");
var timingList = RequireField(majsonType, "timingList").GetValue(majson)!;
var addTiming = timingList.GetType().GetMethod("Add")
                ?? throw new MissingMethodException("Majson.timingList.Add");

foreach (var timing in sourceNotes)
{
    if (timing is null) continue;
    var timingType = timing.GetType();
    var parsedNotes = timingType.GetMethod("getNotes", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)!
        .Invoke(timing, null);
    RequireField(timingType, "noteList").SetValue(timing, parsedNotes);
    addTiming.Invoke(timingList, new[] { timing });
}

SetField(majsonType, majson, "title", GetStaticString(simaiType, "title"));
SetField(majsonType, majson, "artist", GetStaticString(simaiType, "artist"));
SetField(majsonType, majson, "designer", ReadDifficultyField(maidataPath, $"des_{difficulty}"));
var levels = (string?[])RequireField(simaiType, "levels").GetValue(null)!;
SetField(majsonType, majson, "level", levels[difficulty - 1] ?? "0");
SetField(majsonType, majson, "difficulty", DifficultyName(difficulty));
SetField(majsonType, majson, "diffNum", difficulty - 1);

Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);
var options = new JsonSerializerOptions { IncludeFields = true };
File.WriteAllText(outputPath, JsonSerializer.Serialize(majson, majsonType, options));
Console.WriteLine(outputPath);
return 0;

static Type RequireType(Assembly assembly, string name) =>
    assembly.GetType(name, throwOnError: true)!;

static FieldInfo RequireField(Type majdataType, string name) =>
    majdataType.GetField(name, BindingFlags.Static | BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
    ?? throw new MissingFieldException(majdataType.FullName, name);

static object? InvokeStatic(Type type, string method, params object?[] values) =>
    type.GetMethod(method, BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic)!
        .Invoke(null, values);

static void SetField(Type majdataType, object majdata, string name, object value) =>
    RequireField(majdataType, name).SetValue(majdata, value);

static string GetStaticString(Type type, string name) =>
    RequireField(type, name).GetValue(null)?.ToString() ?? "";

static string ReadDifficultyField(string path, string field)
{
    var prefix = $"&{field}=";
    return File.ReadLines(path)
        .FirstOrDefault(line => line.StartsWith(prefix, StringComparison.Ordinal))?
        .Substring(prefix.Length).Trim() ?? "";
}

static string DifficultyName(int difficulty) => difficulty switch
{
    1 => "EASY",
    2 => "BASIC",
    3 => "ADVANCED",
    4 => "EXPERT",
    5 => "MASTER",
    6 => "Re:MASTER",
    7 => "ORIGINAL",
    _ => "DEFAULT",
};
