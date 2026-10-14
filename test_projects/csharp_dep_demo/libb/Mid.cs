namespace DemoLibB;

public static class Mid
{
    public static string Call()
    {
        return DemoLibC.Vuln.Run();
    }
}
